from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.agents import team as agents
from app.db import init_db
from app.db.models import LibraryRow, MessageRow, ReportRow, SessionLocal, SessionRow, SnapshotRow, TradeRow
from app.orchestrator.engine import orchestrator
from app.schemas.models import FloorChatRequest, SessionStatus, SessionView, StartSessionRequest
from app.services.cost import cost_tracker
from app.services.telegram import discover_telegram_chat_id, resolve_chat_id, send_telegram_bg
from app.config import reload_settings

app = FastAPI(title="Quack Multi-Agent Trader", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_ws_clients: set[WebSocket] = set()


async def _broadcast(event: dict[str, Any]) -> None:
    dead = []
    data = json.dumps(event, default=str)
    for ws in list(_ws_clients):
        try:
            await asyncio.wait_for(ws.send_text(data), timeout=1.0)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


@app.on_event("startup")
async def startup() -> None:
    await init_db()
    # Only reset in-memory desk if no live task (preserve session across code reload when possible)
    if orchestrator._task is None or orchestrator._task.done():
        orchestrator.status = SessionStatus.IDLE
        orchestrator.phase = "idle"
        # Keep session_id pointing at latest DB session for UI reconnect / history
        async with SessionLocal() as db:
            latest = (
                await db.execute(select(SessionRow).order_by(SessionRow.id.desc()).limit(1))
            ).scalar_one_or_none()
            if latest and latest.status in {
                SessionStatus.RUNNING.value,
                SessionStatus.PREOPEN.value,
                SessionStatus.OPEN.value,
                SessionStatus.CLOSING.value,
                SessionStatus.PAUSED.value,
            }:
                # Mark abandoned crash sessions stopped so UI doesn't look "live" forever
                latest.status = SessionStatus.STOPPED.value
                latest.phase = "interrupted"
                await db.commit()
                orchestrator.session_id = latest.id
            else:
                orchestrator.session_id = latest.id if latest else None
        orchestrator.broker = None
        orchestrator._stop.clear()
        orchestrator._pause.clear()
    orchestrator.subscribe(_broadcast)
    cost_tracker.subscribe(_broadcast)
    settings = reload_settings()
    if settings.telegram_chat_id:
        cost_tracker.set_telegram_chat_id(str(settings.telegram_chat_id))
    await discover_telegram_chat_id()
    cid = resolve_chat_id()
    if cid:
        send_telegram_bg(
            f"Quack desk online.\nTelegram chat {cid} linked.\n"
            "Alerts: brief/plan, votes, trades, major phases, pause/stop, EOD, budget, errors."
        )


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "status": orchestrator.status.value,
        "phase": orchestrator.phase,
        "session_id": orchestrator.session_id,
        "broker": orchestrator.broker.name if orchestrator.broker else None,
        "budget": cost_tracker.snapshot(),
        "paused": orchestrator._pause.is_set(),
        "floor": orchestrator.floor,
    }


@app.get("/api/budget")
async def budget():
    return cost_tracker.snapshot()


@app.get("/api/floor")
async def floor_state():
    return orchestrator.floor


@app.post("/api/budget/reset")
async def budget_reset():
    cost_tracker.reset_budget()
    snap = cost_tracker.snapshot()
    await _broadcast({"type": "budget", "data": snap})
    return snap


@app.post("/api/telegram/discover")
async def telegram_discover():
    cid = await discover_telegram_chat_id()
    if not cid:
        return {
            "ok": False,
            "hint": "Open Telegram, find your Quack bot, send /start, then call this again.",
        }
    ok = await send_telegram("Quack desk linked. You will get trade alerts and budget pause notices here.")
    return {"ok": True, "chat_id": cid, "test_message_sent": ok}


@app.post("/api/session/start")
async def start_session(req: StartSessionRequest):
    try:
        row = await orchestrator.start(req)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to start session: {e}") from e
    brief = orchestrator.brief.model_dump(mode="json") if orchestrator.brief else None
    return {
        "id": row.id,
        "status": row.status,
        "phase": row.phase,
        "capital_usd": row.capital_usd,
        "start_at": row.start_at,
        "end_at": row.end_at,
        "prompt": orchestrator.user_prompt,
        "brief": brief,
        "budget": cost_tracker.snapshot(),
    }


@app.post("/api/session/stop")
async def stop_session():
    await orchestrator.stop()
    return {"ok": True, "status": orchestrator.status.value, "phase": orchestrator.phase}


@app.post("/api/session/pause")
async def pause_session():
    try:
        result = await orchestrator.pause_agents()
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"ok": True, "status": orchestrator.status.value, "phase": orchestrator.phase, **result}


@app.post("/api/session/resume")
async def resume_session():
    try:
        result = await orchestrator.resume_agents()
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"ok": True, "status": orchestrator.status.value, "phase": orchestrator.phase, **result}


@app.get("/api/session/current")
async def current_session():
    if not orchestrator.session_id:
        return {"session": None}
    async with SessionLocal() as db:
        row = await db.get(SessionRow, orchestrator.session_id)
        if not row:
            return {"session": None}
        return {
            "session": SessionView(
                id=row.id,
                status=row.status,  # type: ignore
                capital_usd=row.capital_usd,
                start_at=row.start_at,
                end_at=row.end_at,
                max_position_pct=row.max_position_pct,
                max_positions_per_day=row.max_positions_per_day,
                flatten_at_close=row.flatten_at_close,
                created_at=row.created_at,
                phase=row.phase,
                last_error=row.last_error,
            )
        }


@app.get("/api/messages")
async def messages(session_id: int | None = None, limit: int = 200):
    sid = session_id or orchestrator.session_id
    if not sid:
        return {"messages": []}
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(MessageRow).where(MessageRow.session_id == sid).order_by(MessageRow.id.desc()).limit(limit)
            )
        ).scalars().all()
        rows = list(reversed(rows))
        return {
            "messages": [
                {
                    "id": r.id,
                    "session_id": r.session_id,
                    "ts": r.ts,
                    "from_agent": r.from_agent,
                    "to_agent": r.to_agent,
                    "kind": r.kind,
                    "content": r.content,
                    "payload": r.payload,
                }
                for r in rows
            ]
        }


@app.get("/api/trades")
async def trades(session_id: int | None = None):
    sid = session_id or orchestrator.session_id
    if not sid:
        return {"trades": []}
    async with SessionLocal() as db:
        rows = (
            await db.execute(select(TradeRow).where(TradeRow.session_id == sid).order_by(TradeRow.id.desc()))
        ).scalars().all()
        return {
            "trades": [
                {
                    "id": r.id,
                    "symbol": r.symbol,
                    "side": r.side,
                    "qty": r.qty,
                    "price": r.price,
                    "allocation_pct": r.allocation_pct,
                    "reason": r.reason,
                    "status": r.status,
                    "source": r.source,
                    "ts": r.ts,
                    "broker_ref": r.broker_ref,
                }
                for r in rows
            ]
        }


@app.get("/api/portfolio")
async def portfolio():
    if not orchestrator.broker:
        return {
            "cash_used": 0,
            "cash_unused": orchestrator.capital,
            "total_equity": orchestrator.capital,
            "day_pnl": 0,
            "day_pnl_pct": 0,
            "positions": [],
        }
    snap = await orchestrator.broker.get_portfolio(orchestrator.capital)
    return snap.model_dump(mode="json")


@app.get("/api/charts/series")
async def chart_series(session_id: int | None = None):
    """Equity / cash / per-position time series for the performance panel."""
    sid = session_id or orchestrator.session_id
    if not sid:
        return {"points": [], "symbols": [], "capital": orchestrator.capital}
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(SnapshotRow).where(SnapshotRow.session_id == sid).order_by(SnapshotRow.id.asc())
            )
        ).scalars().all()
        session = await db.get(SessionRow, sid)
        capital = session.capital_usd if session else orchestrator.capital

    points = []
    symbols: set[str] = set()
    peak = capital
    for r in rows:
        peak = max(peak, float(r.equity or 0))
        equity = float(r.equity or 0)
        drawdown_pct = ((equity - peak) / peak * 100) if peak else 0.0
        return_pct = ((equity - capital) / capital * 100) if capital else float(r.day_pnl_pct or 0)
        pos_map = {}
        for p in r.positions or []:
            sym = str(p.get("symbol") or "")
            if not sym:
                continue
            symbols.add(sym)
            pos_map[sym] = {
                "market_value": float(p.get("market_value") or 0),
                "unrealized_pnl": float(p.get("unrealized_pnl") or 0),
                "unrealized_pnl_pct": float(p.get("unrealized_pnl_pct") or 0),
                "allocation_pct": float(p.get("allocation_pct") or 0),
                "side": p.get("side"),
            }
        points.append(
            {
                "ts": r.ts.isoformat() if r.ts else None,
                "equity": equity,
                "cash_used": float(r.cash_used or 0),
                "cash_unused": float(r.cash_unused or 0),
                "day_pnl": float(r.day_pnl or 0),
                "day_pnl_pct": float(r.day_pnl_pct or 0),
                "return_pct": return_pct,
                "drawdown_pct": drawdown_pct,
                "positions": pos_map,
            }
        )

    # Also include symbols from trades even if flat now
    async with SessionLocal() as db:
        trade_syms = (
            await db.execute(select(TradeRow.symbol).where(TradeRow.session_id == sid).distinct())
        ).scalars().all()
        symbols.update(trade_syms)

    return {
        "session_id": sid,
        "capital": capital,
        "symbols": sorted(symbols),
        "points": points,
    }


@app.get("/api/library")
async def library():
    async with SessionLocal() as db:
        rows = (await db.execute(select(LibraryRow).order_by(LibraryRow.trade_date.desc()))).scalars().all()
        return {
            "days": [
                {
                    "id": r.id,
                    "session_id": r.session_id,
                    "trade_date": r.trade_date,
                    "total_pnl": r.total_pnl,
                    "total_pnl_pct": r.total_pnl_pct,
                    "best_symbol": r.best_symbol,
                    "worst_symbol": r.worst_symbol,
                    "pros": r.pros,
                    "cons": r.cons,
                    "mistakes": r.mistakes,
                    "conclusions": r.conclusions,
                    "agent_scores": r.agent_scores,
                    "full_report": r.full_report,
                    "simulated_after_hours": bool((r.full_report or {}).get("simulated_after_hours")),
                    "execution_note": (r.full_report or {}).get("execution_note") or "",
                    "execution_venue": (r.full_report or {}).get("execution_venue") or "",
                }
                for r in rows
            ]
        }


@app.get("/api/library/{trade_date}")
async def library_day(trade_date: str):
    async with SessionLocal() as db:
        row = (await db.execute(select(LibraryRow).where(LibraryRow.trade_date == trade_date))).scalar_one_or_none()
        if not row:
            return {"error": "not found"}
        return {
            "id": row.id,
            "session_id": row.session_id,
            "trade_date": row.trade_date,
            "total_pnl": row.total_pnl,
            "total_pnl_pct": row.total_pnl_pct,
            "best_symbol": row.best_symbol,
            "worst_symbol": row.worst_symbol,
            "pros": row.pros,
            "cons": row.cons,
            "mistakes": row.mistakes,
            "conclusions": row.conclusions,
            "agent_scores": row.agent_scores,
            "full_report": row.full_report,
            "simulated_after_hours": bool((row.full_report or {}).get("simulated_after_hours")),
            "execution_note": (row.full_report or {}).get("execution_note") or "",
            "execution_venue": (row.full_report or {}).get("execution_venue") or "",
        }


def _ticker_from_report(report: dict, symbol: str) -> dict | None:
    for t in report.get("tickers") or []:
        if str(t.get("symbol", "")).upper() == symbol.upper():
            return t
    return None


@app.get("/api/symbol/{symbol}")
async def symbol_detail(symbol: str, session_id: int | None = None):
    sid = session_id or orchestrator.session_id
    symbol = symbol.upper()
    position = None
    if orchestrator.broker:
        snap = await orchestrator.broker.get_portfolio(orchestrator.capital)
        position = next((p.model_dump(mode="json") for p in snap.positions if p.symbol.upper() == symbol), None)

    agent_views: dict[str, Any] = {}
    trades: list[dict] = []
    plan_trade = None
    if sid:
        async with SessionLocal() as db:
            reports = (
                await db.execute(select(ReportRow).where(ReportRow.session_id == sid).order_by(ReportRow.id.desc()))
            ).scalars().all()
            seen = set()
            for r in reports:
                if r.agent in seen:
                    continue
                seen.add(r.agent)
                hit = _ticker_from_report(r.content or {}, symbol)
                if hit:
                    agent_views[r.agent] = hit
                elif r.agent == "Strategist":
                    for t in (r.content or {}).get("trades") or []:
                        if str(t.get("symbol", "")).upper() == symbol:
                            plan_trade = t
                            agent_views["Strategist"] = {
                                "summary": (r.content or {}).get("narrative"),
                                "trade": t,
                                "score": None,
                                "bias": t.get("side"),
                            }
            trade_rows = (
                await db.execute(select(TradeRow).where(TradeRow.session_id == sid, TradeRow.symbol == symbol))
            ).scalars().all()
            trades = [
                {
                    "id": t.id,
                    "side": t.side,
                    "qty": t.qty,
                    "price": t.price,
                    "allocation_pct": t.allocation_pct,
                    "reason": t.reason,
                    "status": t.status,
                    "source": t.source,
                    "ts": t.ts,
                }
                for t in trade_rows
            ]

    if orchestrator.plan and not plan_trade:
        for t in orchestrator.plan.trades:
            if t.symbol.upper() == symbol:
                plan_trade = t.model_dump()
                break

    return {
        "symbol": symbol,
        "position": position,
        "plan_trade": plan_trade,
        "agent_views": agent_views,
        "trades": trades,
        "motivation": (plan_trade or {}).get("reason") or (position or {}).get("reason") or "",
    }


@app.post("/api/floor/chat")
async def floor_chat(req: FloorChatRequest):
    """Non-blocking commentator — does not interrupt the trading loop."""
    sid = req.session_id or orchestrator.session_id
    portfolio = None
    if orchestrator.broker:
        try:
            portfolio = (
                await asyncio.wait_for(orchestrator.broker.get_portfolio(orchestrator.capital), timeout=5.0)
            ).model_dump(mode="json")
        except Exception:
            portfolio = None
    messages = []
    trades = []
    if sid:
        async with SessionLocal() as db:
            msgs = (
                await db.execute(
                    select(MessageRow).where(MessageRow.session_id == sid).order_by(MessageRow.id.desc()).limit(40)
                )
            ).scalars().all()
            messages = [
                {"from": m.from_agent, "to": m.to_agent, "kind": m.kind, "content": m.content[:500]}
                for m in reversed(list(msgs))
            ]
            trs = (
                await db.execute(select(TradeRow).where(TradeRow.session_id == sid).order_by(TradeRow.id.desc()).limit(20))
            ).scalars().all()
            trades = [
                {"symbol": t.symbol, "side": t.side, "allocation_pct": t.allocation_pct, "reason": t.reason, "status": t.status}
                for t in trs
            ]

    context = {
        "session": {
            "id": sid,
            "status": orchestrator.status.value,
            "phase": orchestrator.phase,
            "capital": orchestrator.capital,
            "prompt": orchestrator.user_prompt,
            "brief": orchestrator.brief.model_dump(mode="json") if orchestrator.brief else None,
            "plan": orchestrator.plan.model_dump() if orchestrator.plan else None,
        },
        "portfolio": portfolio,
        "recent_messages": messages,
        "recent_trades": trades,
        "budget": cost_tracker.snapshot(),
        "universe": "top10",
    }
    try:
        answer = await asyncio.wait_for(agents.ask_floor(req.message, context), timeout=50.0)
    except Exception as e:
        answer = f"Floor busy/unavailable right now ({e}). Desk keeps running."

    # Log commentator exchange without blocking the desk (best-effort)
    if sid:
        async with SessionLocal() as db:
            db.add(
                MessageRow(
                    session_id=sid,
                    from_agent="Operator",
                    to_agent="Floor",
                    kind="commentary",
                    content=req.message,
                )
            )
            db.add(
                MessageRow(
                    session_id=sid,
                    from_agent="Floor",
                    to_agent="Operator",
                    kind="commentary",
                    content=answer,
                )
            )
            await db.commit()

    await _broadcast(
        {
            "type": "floor",
            "question": req.message,
            "answer": answer,
        }
    )
    return {"answer": answer, "agent": "Floor"}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    await ws.send_text(
        json.dumps(
            {
                "type": "hello",
                "status": orchestrator.status.value,
                "phase": orchestrator.phase,
                "session_id": orchestrator.session_id,
                "floor": orchestrator.floor,
                "paused": orchestrator._pause.is_set(),
            }
        )
    )
    try:
        while True:
            # Keep alive; client may send pings
            await ws.receive_text()
    except WebSocketDisconnect:
        _ws_clients.discard(ws)
    except Exception:
        _ws_clients.discard(ws)
