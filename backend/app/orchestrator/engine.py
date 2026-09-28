from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from typing import Any, Callable, Awaitable, Optional

from sqlalchemy import select

from app.agents import team as agents
from app.brokers import build_broker
from app.brokers.base import Broker
from app.data.prices import get_last_price, live_candle_pack, load_top50
from app.db.models import (
    LessonRow,
    LibraryRow,
    MessageRow,
    PositionRow,
    ReportRow,
    SessionLocal,
    SessionRow,
    SnapshotRow,
    TradeRow,
)
from app.schemas.models import DayPlan, ParsedRunBrief, SessionStatus, Side, StartSessionRequest, TradeIntent
from app.services.cost import BudgetExceeded, cost_tracker
from app.services.telegram import (
    format_budget_pause,
    format_eod_summary,
    format_phase,
    format_trade_alert,
    notify_desk,
    send_telegram_bg,
)

Broadcast = Callable[[dict[str, Any]], Awaitable[None]]


class Orchestrator:
    def __init__(self) -> None:
        self.broker: Optional[Broker] = None
        self.session_id: Optional[int] = None
        self.plan: Optional[DayPlan] = None
        self.status = SessionStatus.IDLE
        self.phase = "idle"
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._pause = asyncio.Event()  # set = agents paused (positions kept)
        self._broadcasts: list[Broadcast] = []
        self.capital = 1000.0
        self.max_pct = 0.30
        self.max_names = 10
        self.flatten_at_close = True
        self.start_at: Optional[datetime] = None
        self.end_at: Optional[datetime] = None
        self.brief: Optional[ParsedRunBrief] = None
        self.user_prompt: str = ""
        self._sentiment: dict = {}
        self._technicals: dict = {}
        self._fundamentals: dict = {}
        self._day_trades: list[dict] = []
        self._pause_snapshot: dict[str, Any] = {}
        self._paused_at: Optional[datetime] = None
        self.simulate_after_hours = False
        self.floor: dict[str, Any] = {
            "last": None,
            "current": [],
            "next": [],
            "meeting": None,
        }

    def subscribe(self, cb: Broadcast) -> None:
        self._broadcasts.append(cb)

    async def set_floor(
        self,
        *,
        current: list[dict[str, str]] | None = None,
        next_agents: list[dict[str, str]] | None = None,
        meeting: dict[str, Any] | None = None,
        advance_last: bool = True,
    ) -> None:
        """Publish live floor map: last → current → next (or meeting)."""
        prev_current = self.floor.get("current") or []
        if advance_last and prev_current:
            # Keep the primary previous actor as "last"
            self.floor["last"] = prev_current[0]
        if current is not None:
            self.floor["current"] = current
        if next_agents is not None:
            self.floor["next"] = next_agents
        self.floor["meeting"] = meeting
        await self.emit({"type": "floor", "data": self.floor})

    async def emit(self, event: dict[str, Any]) -> None:
        for cb in list(self._broadcasts):
            try:
                await cb(event)
            except Exception:
                pass

    async def log(
        self,
        from_agent: str,
        content: str,
        to_agent: str | None = None,
        kind: str = "chat",
        payload: dict | None = None,
    ) -> None:
        if not self.session_id:
            return
        async with SessionLocal() as db:
            row = MessageRow(
                session_id=self.session_id,
                from_agent=from_agent,
                to_agent=to_agent,
                kind=kind,
                content=content,
                payload=payload or {},
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            msg = {
                "type": "agent_message",
                "id": row.id,
                "session_id": self.session_id,
                "ts": row.ts.isoformat() if row.ts else datetime.now(timezone.utc).isoformat(),
                "from_agent": from_agent,
                "to_agent": to_agent,
                "kind": kind,
                "content": content,
                "payload": payload or {},
            }
        await self.emit(msg)
        # Mirror important desk events to Telegram without blocking agents
        try:
            await notify_desk(from_agent, content, kind=kind)
        except Exception:
            pass

    async def set_phase(self, phase: str, status: SessionStatus | None = None) -> None:
        self.phase = phase
        if status:
            self.status = status
        if self.session_id:
            async with SessionLocal() as db:
                row = await db.get(SessionRow, self.session_id)
                if row:
                    row.phase = phase
                    if status:
                        row.status = status.value
                    await db.commit()
        await self.emit({"type": "phase", "phase": phase, "status": self.status.value})
        # Only announce major phase changes (skip noisy micro-phases)
        if phase in {
            "starting",
            "preopen_research",
            "planning",
            "opening",
            "committee",
            "executing",
            "live",
            "paused",
            "closing",
            "stopping",
            "stopped",
            "error",
            "budget_paused",
        }:
            send_telegram_bg(format_phase(phase, self.status.value))

    async def start(self, req: StartSessionRequest) -> SessionRow:
        if self._task and not self._task.done():
            raise RuntimeError("Session already running")
        try:
            cost_tracker.ensure_allowed()
        except BudgetExceeded as e:
            raise RuntimeError(
                f"Cannot start: Anthropic budget exceeded (${e.spent:.2f} / ${e.limit:.2f}). "
                "Raise ANTHROPIC_BUDGET_USD or reset budget."
            ) from e

        self._stop.clear()
        self._pause.clear()
        self.plan = None
        self._day_trades = []
        self._sentiment = {}
        self._technicals = {}
        self._fundamentals = {}
        self._pause_snapshot = {}
        self._paused_at = None
        self.simulate_after_hours = False
        self.brief = None
        self.user_prompt = req.prompt.strip()
        self.capital = float(req.capital_usd) if req.capital_usd is not None else 1000.0
        self.max_pct = min(float(req.max_position_pct) if req.max_position_pct is not None else 0.30, 0.30)
        self.max_names = min(int(req.max_positions_per_day) if req.max_positions_per_day is not None else 10, 10)
        self.flatten_at_close = True if req.flatten_at_close is None else bool(req.flatten_at_close)
        now = datetime.now(timezone.utc)
        self.start_at = req.start_at or now
        self.end_at = req.end_at or now
        self.broker = None

        async with SessionLocal() as db:
            row = SessionRow(
                status=SessionStatus.RUNNING.value,
                phase="starting",
                capital_usd=self.capital,
                start_at=self.start_at,
                end_at=self.end_at,
                max_position_pct=self.max_pct,
                max_positions_per_day=self.max_names,
                flatten_at_close=self.flatten_at_close,
                meta={"prompt": self.user_prompt},
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            self.session_id = row.id

        self.status = SessionStatus.RUNNING
        self.phase = "starting"
        await self.set_floor(
            current=[{"agent": "Desk", "task": "Parsing operator brief"}],
            next_agents=[{"agent": "Sentiment", "task": "Research"}, {"agent": "Technicals", "task": "Technicals"}],
            meeting=None,
            advance_last=False,
        )
        await self.emit({"type": "phase", "phase": self.phase, "status": self.status.value})
        await self.log("Desk", "Session started — parsing operator brief…", kind="brief")
        self._task = asyncio.create_task(self._bootstrap_and_run(req))
        return row

    async def _bootstrap_and_run(self, req: StartSessionRequest) -> None:
        try:
            brief = await agents.parse_run_brief(self.user_prompt)
            if req.capital_usd is not None:
                brief.capital_usd = req.capital_usd
            if req.start_at is not None:
                brief.start_at = req.start_at
                brief.start_mode = "scheduled"
            if req.end_at is not None:
                brief.end_at = req.end_at
                brief.end_mode = "until_datetime"
            if req.max_position_pct is not None:
                brief.max_position_pct = min(req.max_position_pct, 0.30)
            if req.max_positions_per_day is not None:
                brief.max_positions_per_day = min(req.max_positions_per_day, 10)
            if req.flatten_at_close is not None:
                brief.flatten_at_close = req.flatten_at_close

            if self._stop.is_set():
                return

            self.brief = brief
            self.capital = brief.capital_usd
            self.max_pct = brief.max_position_pct
            self.max_names = brief.max_positions_per_day
            self.flatten_at_close = brief.flatten_at_close
            now = datetime.now(timezone.utc)
            if brief.start_mode == "immediate":
                self.start_at = now
            elif brief.start_mode == "wait_next_open":
                self.start_at = brief.start_at or now
            else:
                self.start_at = brief.start_at or now
            self.end_at = brief.end_at or now

            async with SessionLocal() as db:
                row = await db.get(SessionRow, self.session_id)
                if row:
                    row.capital_usd = self.capital
                    row.start_at = self.start_at
                    row.end_at = self.end_at
                    row.max_position_pct = self.max_pct
                    row.max_positions_per_day = self.max_names
                    row.flatten_at_close = self.flatten_at_close
                    row.meta = {
                        "prompt": self.user_prompt,
                        "brief": brief.model_dump(mode="json"),
                    }
                    await db.commit()

            self.broker = await build_broker(
                starting_cash=self.capital,
                force_simulate=bool(brief.simulate_after_hours),
            )
            self.simulate_after_hours = bool(brief.simulate_after_hours) or (
                self.broker is not None and self.broker.name == "mock"
            )
            if self.simulate_after_hours:
                await self.log(
                    "Desk",
                    "After-hours / closed-market mode: using SIMULATED paper broker (yfinance marks). "
                    "Fills are NOT live Trading212 market executions.",
                    kind="brief",
                )
            await self.log(
                "Desk",
                f"Operator prompt received:\n«{self.user_prompt[:1200]}»",
                kind="prompt",
                payload={"prompt": self.user_prompt},
            )
            await self.log(
                "Desk",
                brief.summary_for_desk
                + f" Capital ${self.capital:.0f}. Window {self.start_at} → {self.end_at}. Broker={self.broker.name}. Universe=top10.",
                kind="brief",
                payload=brief.model_dump(mode="json"),
            )
            await self._run_loop()
        except BudgetExceeded as e:
            await self._handle_budget_exceeded(e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self.log("Desk", f"Session failed during startup: {e}", kind="error")
            await self.set_phase("error", SessionStatus.ERROR)
            await self.emit({"type": "error", "message": str(e)})

    async def stop(self) -> None:
        self._pause.clear()
        self._stop.set()
        await self.set_phase("stopping", SessionStatus.STOPPED)
        await self.log("Desk", "Stop & flatten requested. Closing all positions and shutting agents down.")
        if self.broker:
            await self.broker.flatten_all()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=30)
            except Exception:
                self._task.cancel()

    async def pause_agents(self) -> dict[str, Any]:
        """Stop agent work but keep positions, trades, and logs."""
        if not self._task or self._task.done():
            raise RuntimeError("No active session to pause")
        if self._pause.is_set():
            return {"paused": True, "already": True}
        if self.broker:
            snap = await self.broker.get_portfolio(self.capital)
            self._pause_snapshot = snap.model_dump(mode="json")
        else:
            self._pause_snapshot = {}
        self._paused_at = datetime.now(timezone.utc)
        self._pause.set()
        await self.set_phase("paused", SessionStatus.PAUSED)
        await self.log(
            "Desk",
            "Agents PAUSED by operator. Positions held. Desk log stays live. Press Continue to resume.",
            kind="pause",
            payload={"snapshot": self._pause_snapshot, "paused_at": self._paused_at.isoformat()},
        )
        await self.emit({"type": "paused", "paused": True, "snapshot": self._pause_snapshot})
        return {"paused": True, "positions": len((self._pause_snapshot or {}).get("positions") or [])}

    async def resume_agents(self) -> dict[str, Any]:
        """Resume agents; analyze changes since pause, then continue process."""
        if not self._task or self._task.done():
            raise RuntimeError("No active session to continue")
        if not self._pause.is_set():
            return {"paused": False, "already": True}
        await self.log("Desk", "Continue pressed — analyzing changes since pause, then agents resume.")
        await self._resume_delta_analysis()
        self._pause.clear()
        await self.set_phase("live", SessionStatus.OPEN)
        await self.log("Desk", "Agents CONTINUED. Picking up from live loop.")
        await self.emit({"type": "paused", "paused": False})
        return {"paused": False}

    async def _resume_delta_analysis(self) -> None:
        if not self.broker:
            return
        now_snap = await self.broker.get_portfolio(self.capital)
        await self.emit({"type": "portfolio", "data": now_snap.model_dump(mode="json")})
        watch = {p.symbol for p in now_snap.positions}
        candles = await live_candle_pack(sorted(watch)[:12]) if watch else {}
        old_pos = {(p.get("symbol"), p.get("side")): p for p in (self._pause_snapshot.get("positions") or [])}
        new_pos = {(p.symbol, p.side.value): p for p in now_snap.positions}
        changes = []
        for key, p in new_pos.items():
            prev = old_pos.get(key)
            if not prev:
                changes.append(f"New vs pause: {p.side.value} {p.symbol}")
                continue
            d_pnl = p.unrealized_pnl - float(prev.get("unrealized_pnl") or 0)
            d_px = p.last_price - float(prev.get("last_price") or p.last_price)
            changes.append(
                f"{p.symbol}: px Δ{d_px:+.2f}, uPnL Δ{d_pnl:+.2f} (now {p.unrealized_pnl_pct:.2f}%)"
            )
        for key, prev in old_pos.items():
            if key not in new_pos:
                changes.append(f"Gone since pause: {prev.get('side')} {prev.get('symbol')}")
        paused_for = ""
        if self._paused_at:
            secs = (datetime.now(timezone.utc) - self._paused_at).total_seconds()
            paused_for = f" Paused ~{int(secs // 60)}m {int(secs % 60)}s."
        await self.log(
            "Book",
            f"Resume snapshot:{paused_for} day PnL ${now_snap.day_pnl:.2f}. Changes: "
            + ("; ".join(changes) if changes else "no structural position changes."),
            kind="data",
            payload={"before": self._pause_snapshot, "after": now_snap.model_dump(mode="json"), "candles": candles},
        )
        check = await agents.run_risk_check(
            {
                **now_snap.model_dump(mode="json"),
                "resume_delta": changes,
                "pause_snapshot": self._pause_snapshot,
            },
            self.plan,
            live_candles=candles,
        )
        await self.log(
            "Risk",
            f"Post-pause risk scan: {check.get('notes') or 'reviewed'} · actions={len(check.get('actions') or [])}",
            payload=check,
        )
        for action in check.get("actions") or []:
            symbol = action.get("symbol")
            typ = action.get("type")
            reason = action.get("reason") or "post-pause"
            if typ == "close" and symbol and self.broker:
                vote = await agents.committee_vote(None, "close", {"reason": reason, "resume": True})
                await self.log(
                    "Desk",
                    f"Resume vote close {symbol}: {vote.reason} → {'GO' if vote.approved else 'NO'}",
                    kind="vote",
                )
                if vote.approved:
                    res = await self.broker.close_position(symbol)
                    await self.log("Executor", f"Closed {symbol} after resume: {res.message}")
                    await self._emit_portfolio()

    async def _wait_while_paused(self) -> None:
        while self._pause.is_set() and not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                if self.broker:
                    try:
                        snap = await self.broker.get_portfolio(self.capital)
                        await self.emit({"type": "portfolio", "data": snap.model_dump(mode="json")})
                    except Exception:
                        pass

    async def _lessons(self) -> list[str]:
        async with SessionLocal() as db:
            rows = (await db.execute(select(LessonRow).order_by(LessonRow.id.desc()).limit(40))).scalars().all()
            return [r.lesson for r in rows]

    async def _save_report(self, agent: str, title: str, content: dict) -> None:
        if not self.session_id:
            return
        async with SessionLocal() as db:
            db.add(
                ReportRow(
                    session_id=self.session_id,
                    trade_date=date.today().isoformat(),
                    agent=agent,
                    title=title,
                    content=content,
                )
            )
            await db.commit()

    async def _record_trade(self, intent: TradeIntent, result: Any) -> None:
        if not self.session_id:
            return
        async with SessionLocal() as db:
            db.add(
                TradeRow(
                    session_id=self.session_id,
                    trade_date=date.today().isoformat(),
                    symbol=intent.symbol,
                    side=intent.side.value,
                    qty=result.qty,
                    price=result.price,
                    allocation_pct=intent.allocation_pct,
                    reason=intent.reason,
                    broker_ref=result.broker_ref,
                    source=result.source,
                    status="filled" if result.ok else "rejected",
                    meta={"message": result.message},
                )
            )
            if result.ok:
                db.add(
                    PositionRow(
                        session_id=self.session_id,
                        symbol=intent.symbol,
                        side=intent.side.value,
                        qty=result.qty,
                        avg_price=result.price,
                        allocation_pct=intent.allocation_pct,
                        reason=intent.reason,
                        source=result.source,
                        is_open=True,
                    )
                )
            await db.commit()
        self._day_trades.append(
            {
                "symbol": intent.symbol,
                "side": intent.side.value,
                "allocation_pct": intent.allocation_pct,
                "reason": intent.reason,
                "price": result.price,
                "ok": result.ok,
            }
        )

    async def _preopen_research(self) -> None:
        await self.set_phase("preopen_research", SessionStatus.PREOPEN)
        lessons = await self._lessons()
        symbols = load_top50()

        await self.set_floor(
            current=[
                {"agent": "Sentiment", "task": "News & sentiment sweep"},
                {"agent": "Technicals", "task": "Independent technicals"},
                {"agent": "Fundamentals", "task": "Fundamentals & events"},
            ],
            next_agents=[{"agent": "Strategist", "task": "Day plan synthesis"}],
            meeting=None,
        )
        await self.log("Desk", "Launching Sentiment, Technicals, Fundamentals in parallel (research before any orders).")
        await self.log("Sentiment", "Starting multi-source news sweep (BBC, Reuters, Bloomberg RSS, FT, CNBC, MarketWatch, Yahoo, Google News…).")
        await self.log("Technicals", "Starting independent technical scan — no access to Sentiment.")
        await self.log("Fundamentals", "Pulling fundamentals, ratios, earnings/dividend calendar.")

        sentiment_t = asyncio.create_task(agents.run_sentiment(symbols, lessons))
        technicals_t = asyncio.create_task(agents.run_technicals(symbols, lessons))
        fundamentals_t = asyncio.create_task(agents.run_fundamentals(symbols, lessons))
        results = await asyncio.gather(sentiment_t, technicals_t, fundamentals_t, return_exceptions=True)

        def _ok(name: str, res: Any) -> dict:
            if isinstance(res, Exception):
                return {"agent": name, "tickers": [], "macro": "", "error": str(res), "raw_notes": f"{name} failed: {res}"}
            return res if isinstance(res, dict) else {"agent": name, "tickers": [], "error": "bad result"}

        self._sentiment = _ok("Sentiment", results[0])
        self._technicals = _ok("Technicals", results[1])
        self._fundamentals = _ok("Fundamentals", results[2])
        for name, pack in (("Sentiment", self._sentiment), ("Technicals", self._technicals), ("Fundamentals", self._fundamentals)):
            if pack.get("error"):
                await self.log(name, f"Research degraded: {pack['error']}", kind="error")
            elif pack.get("degraded"):
                await self.log(name, f"Research partial/heuristic: {(pack.get('raw_notes') or '')[:240]}", kind="warn")
            else:
                await self.log(name, f"Package ok — {len(pack.get('tickers') or [])} tickers.")

        await self._save_report("Sentiment", "Preopen sentiment", self._sentiment)
        await self._save_report("Technicals", "Preopen technicals", self._technicals)
        await self._save_report("Fundamentals", "Preopen fundamentals", self._fundamentals)

        await self.log("Sentiment", f"Report ready for {len(self._sentiment.get('tickers') or [])} names. Macro: {(self._sentiment.get('macro') or '')[:240]}")
        await self.log("Technicals", f"TA report ready for {len(self._technicals.get('tickers') or [])} names.")
        await self.log("Fundamentals", f"Fundamentals ready for {len(self._fundamentals.get('tickers') or [])} companies.")

        await self.set_floor(
            current=[{"agent": "Strategist", "task": "Building day plan from research"}],
            next_agents=[{"agent": "Committee", "task": "Open vote: Strategist · Risk · Desk"}],
        )
        await self.log("Strategist", "Sentiment, Technicals, Fundamentals — send compressed packages. Building day plan from operator brief.")
        self.plan = await agents.run_strategist(
            self._sentiment,
            self._technicals,
            self._fundamentals,
            self.capital,
            self.max_pct,
            self.max_names,
            lessons,
            operator_brief=self.brief,
        )
        await self._save_report("Strategist", "Day plan", self.plan.model_dump())
        await self.log(
            "Strategist",
            f"Day plan: {len(self.plan.trades)} trades. {self.plan.narrative[:400]}",
            kind="plan",
            payload=self.plan.model_dump(),
        )

        await self.set_floor(
            current=[
                {"agent": "Strategist", "task": "Voting"},
                {"agent": "Risk", "task": "Voting"},
                {"agent": "Desk", "task": "Voting"},
            ],
            next_agents=[{"agent": "Executor", "task": "Execute approved opens"}],
            meeting={
                "members": ["Strategist", "Risk", "Desk"],
                "topic": "Open trades for the session",
            },
        )
        await self.log("Desk", "Open committee: Strategist, Risk, Desk reviewing Strategist plan.")
        approved: list[TradeIntent] = []
        for trade in self.plan.trades:
            await self.set_floor(
                current=[
                    {"agent": "Strategist", "task": f"Vote {trade.side.value} {trade.symbol}"},
                    {"agent": "Risk", "task": f"Vote {trade.side.value} {trade.symbol}"},
                    {"agent": "Desk", "task": f"Vote {trade.side.value} {trade.symbol}"},
                ],
                next_agents=[{"agent": "Executor", "task": "Execute if approved"}],
                meeting={
                    "members": ["Strategist", "Risk", "Desk"],
                    "topic": f"{trade.side.value.upper()} {trade.symbol} · {trade.allocation_pct*100:.0f}%",
                },
            )
            result = await agents.committee_vote(trade, "open", {"portfolio_cash": self.capital, "plan_narrative": self.plan.narrative})
            vote_txt = ", ".join(f"{v.voter}={v.action}" for v in result.votes)
            await self.log(
                "Desk",
                f"Vote on {trade.side.value} {trade.symbol}: {vote_txt} → {'APPROVED' if result.approved else 'REJECTED'}",
                kind="vote",
            )
            if result.approved and result.trade:
                approved.append(result.trade)

        self.plan = DayPlan(trades=approved, narrative=self.plan.narrative, priorities=self.plan.priorities)
        await self.log("Executor", f"Standing by to execute {len(approved)} approved opens.")

    async def _execute_opens(self) -> None:
        await self.set_phase("opening", SessionStatus.OPEN)
        await self.set_floor(
            current=[{"agent": "Executor", "task": "Sending approved orders"}],
            next_agents=[{"agent": "Book", "task": "Live book feed"}],
            meeting=None,
        )
        if not self.broker or not self.plan:
            return
        force_now = bool(
            self.brief
            and (
                self.brief.trade_immediately_after_research
                or self.brief.start_mode == "immediate"
                or self.brief.force_entry_timing == "intraday"
            )
        )
        for trade in self.plan.trades:
            if trade.entry_timing == "wait" and not force_now:
                await self.log("Executor", f"Deferring {trade.symbol} (wait for range) — will revisit intraday.")
                continue
            if force_now and trade.entry_timing == "wait":
                await self.log("Executor", f"Operator said trade now — executing deferred {trade.symbol} immediately.")
            notional = self.capital * trade.allocation_pct
            await self.log("Executor", f"Sending {trade.side.value} {trade.symbol} notional ${notional:.2f} ({trade.allocation_pct*100:.1f}%).")
            # Seed mock prices from live Yahoo/Finnhub/FMP — never invent $100
            if hasattr(self.broker, "set_price"):
                px = await get_last_price(trade.symbol)
                if not px:
                    await self.log(
                        "Executor",
                        f"SKIP {trade.symbol}: no real market quote available (Yahoo/FMP/Finnhub failed).",
                        kind="error",
                    )
                    continue
                self.broker.set_price(trade.symbol, px)  # type: ignore
                await self.log("Book", f"Mark {trade.symbol} @ ${px:.2f} (Yahoo chart / delayed).", kind="data")
            result = await self.broker.market_order(trade.symbol, trade.side, notional, trade.reason)
            await self._record_trade(trade, result)
            if result.ok:
                await self.log("Executor", f"Filled {trade.side.value} {trade.symbol} @ {result.price:.2f} via {result.source}.")
                send_telegram_bg(
                    format_trade_alert(
                        ts=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                        symbol=trade.symbol,
                        side=trade.side.value,
                        allocation_pct=trade.allocation_pct,
                        reason=trade.reason,
                        simulated=self.simulate_after_hours,
                    )
                )
            else:
                await self.log("Executor", f"REJECTED {trade.symbol}: {result.message}")
            await self._emit_portfolio()

    async def _record_snapshot(self, snap) -> None:
        if not self.session_id:
            return
        positions = [
            {
                "symbol": p.symbol,
                "side": p.side.value if hasattr(p.side, "value") else p.side,
                "market_value": p.market_value,
                "unrealized_pnl": p.unrealized_pnl,
                "unrealized_pnl_pct": p.unrealized_pnl_pct,
                "allocation_pct": p.allocation_pct,
                "last_price": p.last_price,
                "qty": p.qty,
            }
            for p in snap.positions
        ]
        async with SessionLocal() as db:
            db.add(
                SnapshotRow(
                    session_id=self.session_id,
                    equity=snap.total_equity,
                    cash_used=snap.cash_used,
                    cash_unused=snap.cash_unused,
                    day_pnl=snap.day_pnl,
                    day_pnl_pct=snap.day_pnl_pct,
                    positions=positions,
                )
            )
            await db.commit()
        await self.emit(
            {
                "type": "snapshot",
                "data": {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "equity": snap.total_equity,
                    "cash_used": snap.cash_used,
                    "cash_unused": snap.cash_unused,
                    "day_pnl": snap.day_pnl,
                    "day_pnl_pct": snap.day_pnl_pct,
                    "positions": positions,
                },
            }
        )

    async def _emit_portfolio(self) -> None:
        if not self.broker:
            return
        await self._refresh_marks()
        snap = await self.broker.get_portfolio(self.capital)
        await self.emit({"type": "portfolio", "data": snap.model_dump(mode="json")})
        await self._record_snapshot(snap)

    async def _refresh_marks(self) -> None:
        """Pull fresh delayed quotes into mock broker so book PnL moves."""
        if not self.broker or not hasattr(self.broker, "set_price"):
            return
        try:
            snap = await self.broker.get_portfolio(self.capital)
        except Exception:
            return
        symbols = [p.symbol for p in snap.positions]
        if not symbols:
            return
        from app.data.prices import get_last_prices

        marks = await get_last_prices(symbols)
        for sym, px in marks.items():
            try:
                self.broker.set_price(sym, px)  # type: ignore[attr-defined]
            except Exception:
                pass

    async def _live_loop(self) -> None:
        await self.set_phase("live", SessionStatus.OPEN)
        await self.set_floor(
            current=[{"agent": "Book", "task": "Polling portfolio & candles"}],
            next_agents=[{"agent": "Risk", "task": "Risk scan"}],
            meeting=None,
        )
        await self.log("Book", "Live data feed online — polling portfolio and watchlist.")
        while not self._stop.is_set():
            if self._pause.is_set():
                await self._wait_while_paused()
                if self._stop.is_set():
                    break
                continue

            now = datetime.now(timezone.utc)
            if self.end_at and now >= self.end_at:
                await self.log("Desk", "Run window ended.")
                break
            if self._should_flatten_now(now):
                await self._close_day()
                # After close, wait until next preopen or end
                await self._wait_until_next_session_or_stop()
                if self._stop.is_set():
                    break
                if self.end_at and datetime.now(timezone.utc) >= self.end_at:
                    break
                await self._preopen_research()
                await self._execute_opens()
                await self.set_phase("live", SessionStatus.OPEN)
                continue

            assert self.broker
            try:
                cost_tracker.ensure_allowed()
            except BudgetExceeded as e:
                await self._handle_budget_exceeded(e)
                break

            await self._refresh_marks()
            snap = await self.broker.get_portfolio(self.capital)
            await self.emit({"type": "portfolio", "data": snap.model_dump(mode="json")})
            await self._record_snapshot(snap)

            # Live (delayed) yfinance candles for open names + plan names
            watch = {p.symbol for p in snap.positions}
            if self.plan:
                watch.update(t.symbol for t in self.plan.trades)
            watch_list = sorted(watch)[:12]
            candles = await live_candle_pack(watch_list) if watch_list else {}

            candle_bits = []
            for sym, pack in list(candles.items())[:5]:
                c5 = (pack or {}).get("5m") or {}
                if "last" in c5:
                    candle_bits.append(f"{sym} 5m={c5['last']:.2f} trend={c5.get('trend')}")
            await self.set_floor(
                current=[{"agent": "Book", "task": "Book + delayed candles"}],
                next_agents=[{"agent": "Risk", "task": "Risk / TP-SL check"}],
                meeting=None,
            )
            await self.log(
                "Book",
                f"Cash used ${snap.cash_used:.2f} | unused ${snap.cash_unused:.2f} | day PnL ${snap.day_pnl:.2f} ({snap.day_pnl_pct:.2f}%) | positions {len(snap.positions)}"
                + (f" | yfinance~15m delayed: {'; '.join(candle_bits)}" if candle_bits else " | yfinance candles: n/a"),
                kind="data",
                payload={"live_candles": candles},
            )

            await self.set_floor(
                current=[{"agent": "Risk", "task": "Scanning positions"}],
                next_agents=[{"agent": "Committee", "task": "Escalate if needed"}],
            )
            check = await agents.run_risk_check(snap.model_dump(mode="json"), self.plan, live_candles=candles)
            for action in check.get("actions") or []:
                symbol = action.get("symbol")
                typ = action.get("type")
                reason = action.get("reason") or ""
                await self.log("Risk", f"Escalation: {typ} {symbol} — {reason}", to_agent="Strategist")
                await self.log("Risk", f"Calling Strategist and Desk to vote on {typ} {symbol}.", to_agent="Desk")
                await self.set_floor(
                    current=[
                        {"agent": "Strategist", "task": f"{typ} {symbol}"},
                        {"agent": "Risk", "task": f"{typ} {symbol}"},
                        {"agent": "Desk", "task": f"{typ} {symbol}"},
                    ],
                    next_agents=[{"agent": "Executor", "task": "Execute if approved"}],
                    meeting={
                        "members": ["Strategist", "Risk", "Desk"],
                        "topic": f"{typ.upper()} {symbol} — {reason[:80]}",
                    },
                )
                pos = next((p for p in snap.positions if p.symbol == symbol), None)
                intent = None
                if typ == "open" and action.get("side"):
                    intent = TradeIntent(
                        symbol=symbol,
                        side=Side(action["side"]),
                        allocation_pct=min(float(action.get("allocation_pct") or 0.1), self.max_pct),
                        entry_timing="intraday",
                        reason=reason,
                    )
                vote = await agents.committee_vote(
                    intent,
                    "close" if typ == "close" else "open",
                    {"reason": reason, "position": pos.model_dump(mode="json") if pos else None},
                )
                await self.log(
                    "Desk",
                    f"Intraday vote {typ} {symbol}: {vote.reason} → {'GO' if vote.approved else 'NO'}",
                    kind="vote",
                )
                if vote.approved and self.broker and typ in ("close", "open"):
                    await self.set_floor(
                        current=[{"agent": "Executor", "task": f"{typ} {symbol}"}],
                        next_agents=[{"agent": "Book", "task": "Refresh book"}],
                        meeting=None,
                    )
                if not vote.approved or not self.broker:
                    continue
                if typ == "close" and symbol:
                    res = await self.broker.close_position(symbol)
                    await self.log("Executor", f"Closed {symbol}: {res.message}")
                    if res.ok:
                        send_telegram_bg(
                            format_trade_alert(
                                ts=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                                symbol=symbol,
                                side="close",
                                allocation_pct=pos.allocation_pct if pos else 0,
                                reason=reason,
                                simulated=self.simulate_after_hours,
                            )
                        )
                elif typ == "open" and vote.trade:
                    notional = self.capital * vote.trade.allocation_pct
                    res = await self.broker.market_order(vote.trade.symbol, vote.trade.side, notional, vote.trade.reason)
                    await self._record_trade(vote.trade, res)
                    if res.ok:
                        send_telegram_bg(
                            format_trade_alert(
                                ts=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                                symbol=vote.trade.symbol,
                                side=vote.trade.side.value,
                                allocation_pct=vote.trade.allocation_pct,
                                reason=vote.trade.reason,
                                simulated=self.simulate_after_hours,
                            )
                        )
                await self._emit_portfolio()

            # Sleep poll interval
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    def _should_flatten_now(self, now: datetime) -> bool:
        if not self.flatten_at_close:
            return False
        # US equity close ~ 20:00 UTC during EDT, 21:00 during EST — use 20:00–20:05 UTC window as default flatten
        # Also flatten if local phase says closing. For simplicity: weekday and hour==20 minute<5 in UTC
        if now.weekday() >= 5:
            return False
        return now.hour == 20 and now.minute < 2 and self.phase == "live"

    async def _wait_until_next_session_or_stop(self) -> None:
        """Idle until next US preopen (~13:30 UTC) or stop/end."""
        await self.set_phase("overnight_idle", SessionStatus.RUNNING)
        await self.log("Desk", "Overnight idle until next pre-open research window.")
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            if self.end_at and now >= self.end_at:
                return
            # Preopen trigger: 13:30–13:35 UTC weekdays (approx 09:30 ET - 60m = 08:30 ET = 12:30/13:30 UTC)
            if now.weekday() < 5 and now.hour == 13 and now.minute < 5:
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    async def _close_day(self) -> None:
        await self.set_phase("closing", SessionStatus.CLOSING)
        await self.log("Desk", "Market close protocol: flatten all positions.")
        results = []
        if self.broker:
            results = await self.broker.flatten_all()
        for r in results:
            await self.log("Executor", f"Flatten {r.symbol}: {r.message}")

        snap = await self.broker.get_portfolio(self.capital) if self.broker else None
        day_ctx = {
            "trades": self._day_trades,
            "plan": self.plan.model_dump() if self.plan else None,
            "final_portfolio": snap.model_dump(mode="json") if snap else {},
            "sentiment_macro": (self._sentiment or {}).get("macro"),
        }
        eod = await agents.run_desk_eod(day_ctx)
        total_pnl = snap.day_pnl if snap else 0.0
        # Best/worst from day's trades / last known
        best = worst = None
        if snap and snap.positions:
            best_p = max(snap.positions, key=lambda p: p.unrealized_pnl)
            worst_p = min(snap.positions, key=lambda p: p.unrealized_pnl)
            best, worst = best_p.symbol, worst_p.symbol
        elif self._day_trades:
            best = self._day_trades[0]["symbol"]
            worst = self._day_trades[-1]["symbol"]

        simulated = bool(self.simulate_after_hours) or (self.broker is not None and self.broker.name == "mock")
        execution_note = (
            "AFTER-HOURS SIMULATION — not Trading212. "
            "This session used a simulated paper broker with yfinance marks. "
            "Agents researched and “traded,” but fills were simulated — not live T212 market executions."
            if simulated
            else "Execution venue: Trading212 demo / live broker path (not after-hours paper simulation)."
        )
        conclusions = (eod.get("conclusions") or "").strip()
        if simulated:
            conclusions = f"{execution_note}\n\n{conclusions}".strip()

        summary = {
            "session_id": self.session_id,
            "trade_date": date.today().isoformat(),
            "total_pnl": total_pnl,
            "total_pnl_pct": (total_pnl / self.capital * 100) if self.capital else 0,
            "best_symbol": best,
            "worst_symbol": worst,
            "pros": eod.get("pros") or [],
            "cons": eod.get("cons") or [],
            "mistakes": eod.get("mistakes") or [],
            "conclusions": conclusions,
            "agent_scores": eod.get("agent_scores") or {},
            "full_report": {
                "eod": eod,
                "trades": self._day_trades,
                "plan": day_ctx.get("plan"),
                "simulated_after_hours": simulated,
                "execution_venue": self.broker.name if self.broker else "unknown",
                "execution_note": execution_note,
            },
        }

        async with SessionLocal() as db:
            # Upsert library by date
            existing = (
                await db.execute(select(LibraryRow).where(LibraryRow.trade_date == summary["trade_date"]))
            ).scalar_one_or_none()
            if existing:
                for k, v in summary.items():
                    if k == "session_id":
                        continue
                    setattr(existing, k, v)
            else:
                db.add(LibraryRow(**{k: v for k, v in summary.items()}))
            for lesson in eod.get("lessons") or []:
                db.add(LessonRow(trade_date=summary["trade_date"], agent="Desk", lesson=lesson))
            await db.commit()

        await self.log(
            "Desk",
            f"Library saved for {summary['trade_date']}. PnL ${total_pnl:.2f}. "
            + ("SIMULATED after-hours — not T212. " if simulated else "")
            + "Scores filed.",
            kind="eod",
            payload=summary,
        )
        send_telegram_bg(
            format_eod_summary(
                {
                    **summary,
                    "simulated_after_hours": simulated,
                    "execution_note": execution_note,
                    "execution_venue": self.broker.name if self.broker else "unknown",
                }
            )
        )
        self._day_trades = []
        await self.emit(
            {
                "type": "library_update",
                "data": {
                    **summary,
                    "simulated_after_hours": simulated,
                    "execution_note": execution_note,
                },
            }
        )

    async def _handle_budget_exceeded(self, e: BudgetExceeded) -> None:
        snap = cost_tracker.snapshot()
        msg = snap.get("pause_reason") or str(e)
        await self.set_phase("budget_paused", SessionStatus.STOPPED)
        await self.log("Desk", msg, kind="budget")
        await self.emit({"type": "budget_exceeded", "message": msg, "data": snap})
        send_telegram_bg(format_budget_pause(snap))
        if self.broker and self.flatten_at_close:
            try:
                await self.broker.flatten_all()
                await self.log("Executor", "Flattened positions after budget pause.")
            except Exception as flat_err:
                await self.log("Executor", f"Flatten after budget pause failed: {flat_err}")
        self._stop.set()

    async def _run_loop(self) -> None:
        try:
            await self.set_phase("waiting_window", SessionStatus.RUNNING)
            # Wait until start_at only when not immediate
            if not (self.brief and self.brief.start_mode == "immediate"):
                while not self._stop.is_set():
                    now = datetime.now(timezone.utc)
                    start = self.start_at
                    if start and start.tzinfo is None:
                        start = start.replace(tzinfo=timezone.utc)
                    if not start or now >= start:
                        break
                    await self.log("Desk", f"Waiting for window start ({start}).")
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=30)
                    except asyncio.TimeoutError:
                        pass

            if self._stop.is_set():
                return

            if self.brief and self.brief.start_mode == "immediate":
                await self.log(
                    "Desk",
                    "Mid-session / immediate start: research now, then trade until the brief end (typically today's close).",
                )

            await self._preopen_research()
            await self._execute_opens()
            await self._live_loop()

            # Final flatten if still open
            if self.broker and self.flatten_at_close:
                await self._close_day()
            await self.set_phase("stopped", SessionStatus.STOPPED)
            await self.log("Desk", "Session complete.")
        except BudgetExceeded as e:
            await self._handle_budget_exceeded(e)
        except Exception as e:
            await self.set_phase("error", SessionStatus.ERROR)
            await self.log("Desk", f"System error: {e}", kind="error")
            if self.session_id:
                async with SessionLocal() as db:
                    row = await db.get(SessionRow, self.session_id)
                    if row:
                        row.last_error = str(e)
                        await db.commit()


orchestrator = Orchestrator()
