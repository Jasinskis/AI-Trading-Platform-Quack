from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Optional

from app.agents.llm import claude_json
from app.agents.prompts import load_prompt
from app.data.fundamentals import batch_fundamentals, sector_averages
from app.data.news import filter_news_for_symbol, gather_market_news, gather_ticker_news
from app.data.prices import get_bars, load_top50, technical_snapshot
from app.schemas.models import (
    CommitteeResult,
    DayPlan,
    ParsedRunBrief,
    Side,
    TradeIntent,
    VoteDecision,
)


def _us_cash_close_utc(now: datetime | None = None) -> datetime:
    """Approximate US cash close in UTC (EDT=20:00, EST=21:00). Uses 20:00 UTC as default."""
    now = now or datetime.now(timezone.utc)
    return now.replace(hour=20, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)


def _is_us_rth(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    # Approx RTH 13:30–20:00 UTC (EDT). Good enough for simulation gating.
    minutes = now.hour * 60 + now.minute
    return 13 * 60 + 30 <= minutes < 20 * 60


def _heuristic_parse_prompt(prompt: str, now: datetime) -> ParsedRunBrief:
    import re

    text = prompt.lower()
    capital = 1000.0
    m = re.search(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", prompt)
    if not m:
        m = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:usd|dollars|eur|euros)", text)
    if m:
        capital = float(m.group(1).replace(",", ""))

    immediate = any(
        k in text
        for k in (
            "right now",
            "trade now",
            "start now",
            "already open",
            "market is open",
            "market already",
            "immediately",
            "asap",
            "from now",
        )
    )
    until_close = any(k in text for k in ("until close", "till close", "to close", "rest of the day", "today only", "until the close"))
    wait_open = any(k in text for k in ("before open", "next open", "tomorrow open", "wait for open"))
    simulate = any(
        k in text
        for k in (
            "simulate",
            "simulation",
            "after hours",
            "after-hours",
            "closing hours",
            "closed market",
            "market is closed",
            "outside market",
            "paper only",
        )
    ) or (not _is_us_rth(now) and immediate)

    close = _us_cash_close_utc(now)
    if close <= now and not simulate:
        from datetime import timedelta

        close = close + timedelta(days=1)
        while close.weekday() >= 5:
            close = close + timedelta(days=1)
    if simulate and (until_close or immediate):
        # Keep a session window even after cash close for paper simulation
        from datetime import timedelta

        if now >= close:
            close = now + timedelta(hours=2)

    start_mode = "immediate"
    if wait_open and not immediate:
        start_mode = "wait_next_open"
    force_timing = "intraday" if immediate or ("already open" in text) or simulate else None

    return ParsedRunBrief(
        capital_usd=capital,
        start_mode=start_mode,
        start_at=now if start_mode == "immediate" else None,
        end_mode="until_close_today" if until_close or start_mode == "immediate" else "until_close_each_day",
        end_at=close,
        flatten_at_close=True,
        trade_immediately_after_research=True,
        force_entry_timing=force_timing,
        simulate_after_hours=simulate,
        objectives=["Follow operator prompt"],
        constraints=["Max 30% per name", "Max 10 names", "Top 10 SPX only"],
        strategy_notes=prompt.strip(),
        summary_for_desk=(
            f"Operator brief accepted. Capital ${capital:.0f}. "
            f"Mode={start_mode}. End={close.isoformat()}. "
            f"{'SIMULATED after-hours paper book. ' if simulate else ''}"
            f"{'Trade as soon as research completes.' if start_mode == 'immediate' else 'Wait for next open.'}"
        ),
        raw_prompt=prompt,
    )


async def parse_run_brief(prompt: str) -> ParsedRunBrief:
    now = datetime.now(timezone.utc)
    close = _us_cash_close_utc(now)
    user = json.dumps(
        {
            "operator_prompt": prompt,
            "now_utc": now.isoformat(),
            "close_utc": close.isoformat(),
            "note": "US cash session roughly 13:30–20:00 UTC (EDT).",
        }
    )
    result = await claude_json(load_prompt("run_brief"), user, max_tokens=1024, timeout=45.0)
    if result.get("mock"):
        return _heuristic_parse_prompt(prompt, now)

    def _dt(val: Any) -> datetime | None:
        if not val:
            return None
        if isinstance(val, datetime):
            return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
        try:
            d = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    brief = ParsedRunBrief(
        capital_usd=float(result.get("capital_usd") or 1000),
        start_mode=result.get("start_mode") or "immediate",
        start_at=_dt(result.get("start_at")) or now,
        end_mode=result.get("end_mode") or "until_close_today",
        end_at=_dt(result.get("end_at")) or close,
        flatten_at_close=bool(result.get("flatten_at_close", True)),
        max_position_pct=min(float(result.get("max_position_pct") or 0.30), 0.30),
        max_positions_per_day=min(int(result.get("max_positions_per_day") or 10), 10),
        skip_full_preopen=bool(result.get("skip_full_preopen", False)),
        trade_immediately_after_research=bool(result.get("trade_immediately_after_research", True)),
        force_entry_timing=result.get("force_entry_timing"),
        simulate_after_hours=bool(result.get("simulate_after_hours")) or (not _is_us_rth(now) and (result.get("start_mode") == "immediate")),
        objectives=result.get("objectives") or [],
        constraints=result.get("constraints") or ["Top 10 SPX only"],
        strategy_notes=result.get("strategy_notes") or prompt,
        summary_for_desk=result.get("summary_for_desk") or "Mission brief parsed.",
        raw_prompt=prompt,
    )
    return brief


def _mock_ticker_reports(agent: str, symbols: list[str], bias_cycle: list[str]) -> dict[str, Any]:
    tickers = []
    for i, s in enumerate(symbols):
        bias = bias_cycle[i % len(bias_cycle)]
        tickers.append(
            {
                "symbol": s,
                "summary": f"[mock {agent}] Compressed view for {s}.",
                "bias": bias,
                "score": 50 + (i % 40),
                "long_term": {"trend": "up" if bias == "bullish" else "down", "notes": "mock"},
                "short_term": {"trend": bias, "notes": "mock"},
                "catalysts": [],
                "events": [],
            }
        )
    return {"agent": agent, "tickers": tickers, "macro": "mock macro calm", "raw_notes": "mock"}


def _bias_from_headlines(items: list[dict[str, Any]]) -> tuple[str, int, str]:
    if not items:
        return "neutral", 45, "No usable headlines in package."
    blob = " ".join(
        f"{n.get('title') or ''} {n.get('summary') or ''}" for n in items
    ).lower()
    bull = sum(1 for w in ("beat", "surge", "rally", "upgrade", "record", "bullish", "growth") if w in blob)
    bear = sum(1 for w in ("miss", "cut", "downgrade", "lawsuit", "probe", "bearish", "slump", "recall") if w in blob)
    if bull > bear + 1:
        return "bullish", min(78, 55 + bull * 5), items[0].get("title") or "News bias bullish"
    if bear > bull + 1:
        return "bearish", min(78, 55 + bear * 5), items[0].get("title") or "News bias bearish"
    return "neutral", 50, items[0].get("title") or "Mixed / thin news"


def _heuristic_sentiment(
    symbols: list[str],
    market: list[dict[str, Any]],
    compact: dict[str, list[dict[str, Any]]],
    note: str,
) -> dict[str, Any]:
    tickers = []
    for s in symbols:
        bias, score, summary = _bias_from_headlines(compact.get(s) or [])
        tickers.append(
            {
                "symbol": s,
                "summary": summary[:240],
                "bias": bias,
                "score": score,
                "catalysts": [],
                "sources": list({n.get("source") for n in (compact.get(s) or []) if n.get("source")}),
            }
        )
    return {
        "agent": "Sentiment",
        "macro": (market[0].get("title") if market else "Macro package thin")[:200],
        "tickers": tickers,
        "raw_notes": f"Heuristic Sentiment fallback: {note}",
        "degraded": True,
    }


def _bias_from_tech(pack: dict[str, Any]) -> tuple[str, int, str, dict, dict]:
    st = pack.get("short_term") or {}
    lt = pack.get("long_term") or {}
    if st.get("error") and lt.get("error"):
        return "neutral", 40, "No bars — data gap", {"trend": "unknown", "notes": "gap"}, {"trend": "unknown", "notes": "gap"}
    st_ret = float(st.get("period_return_pct") or 0)
    lt_trend = st.get("trend") or lt.get("trend") or "neutral"
    if st_ret >= 1.0 or (lt_trend == "up" and st_ret >= 0):
        bias, score = "bullish", min(80, 55 + int(abs(st_ret)))
    elif st_ret <= -1.0 or (lt_trend == "down" and st_ret <= 0):
        bias, score = "bearish", min(80, 55 + int(abs(st_ret)))
    else:
        bias, score = "neutral", 50
    summary = f"ST ret {st_ret:.2f}% · trend {lt_trend}"
    return (
        bias,
        score,
        summary,
        {"trend": lt.get("trend") or lt_trend, "notes": f"ret={lt.get('period_return_pct')}"},
        {"trend": st.get("trend") or lt_trend, "notes": f"ret={st_ret:.2f}"},
    )


def _heuristic_technicals(symbols: list[str], tech: dict[str, Any], note: str) -> dict[str, Any]:
    tickers = []
    for s in symbols:
        bias, score, summary, long_t, short_t = _bias_from_tech(tech.get(s) or {})
        tickers.append(
            {
                "symbol": s,
                "summary": summary,
                "bias": bias,
                "score": score,
                "long_term": long_t,
                "short_term": short_t,
                "volatility": str((tech.get(s) or {}).get("short_term", {}).get("volatility") or ""),
            }
        )
    return {
        "agent": "Technicals",
        "tickers": tickers,
        "raw_notes": f"Heuristic Technicals fallback: {note}",
        "degraded": True,
    }


def _heuristic_fundamentals(symbols: list[str], funds: dict[str, Any], note: str) -> dict[str, Any]:
    tickers = []
    for s in symbols:
        f = funds.get(s) or {}
        err = f.get("error")
        events = f.get("events") or f.get("calendar") or []
        pe = f.get("trailingPE") or f.get("pe")
        tickers.append(
            {
                "symbol": s,
                "summary": (err or f.get("longName") or s)[:200] if err else f"PE={pe}; events={len(events)}",
                "bias": "neutral",
                "score": 48 if err else 55,
                "ratios": {k: f.get(k) for k in ("trailingPE", "priceToBook", "returnOnEquity", "profitMargins") if f.get(k) is not None},
                "vs_sector": "",
                "events": events[:5] if isinstance(events, list) else [],
                "concerns": [err] if err else [],
            }
        )
    return {
        "agent": "Fundamentals",
        "tickers": tickers,
        "raw_notes": f"Heuristic Fundamentals fallback: {note}",
        "degraded": True,
    }


async def _claude_batched(
    system_name: str,
    symbols: list[str],
    build_user: Any,
    *,
    batch_size: int = 5,
    max_tokens: int = 2500,
    timeout: float = 55.0,
) -> dict[str, Any]:
    """Call Claude in small ticker batches and merge tickers[]."""
    agent_label = {"sentiment": "Sentiment", "technicals": "Technicals", "fundamentals": "Fundamentals"}.get(system_name, system_name)
    merged: dict[str, Any] = {"agent": agent_label, "tickers": [], "raw_notes": ""}
    macros: list[str] = []
    notes: list[str] = []
    failed_batches: list[str] = []
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i : i + batch_size]
        user = build_user(batch)
        try:
            part = await claude_json(load_prompt(system_name), user, max_tokens=max_tokens, timeout=timeout)
        except Exception as e:
            failed_batches.append(f"{batch}:{e}")
            continue
        if part.get("mock"):
            return part
        macros.append(str(part.get("macro") or ""))
        notes.append(str(part.get("raw_notes") or ""))
        for t in part.get("tickers") or []:
            if isinstance(t, dict) and t.get("symbol"):
                merged["tickers"].append(t)
    if not merged["tickers"] and failed_batches:
        raise RuntimeError(f"{agent_label} all batches failed: {'; '.join(failed_batches)[:400]}")
    if macros:
        merged["macro"] = " | ".join(m for m in macros if m)[:500]
    note = " ".join(notes)[:400]
    if failed_batches:
        merged["degraded"] = True
        note = (note + " | partial batch failures: " + "; ".join(failed_batches))[:500]
    merged["raw_notes"] = note
    return merged


async def run_sentiment(symbols: list[str] | None = None, lessons: list[str] | None = None) -> dict[str, Any]:
    from app.config import get_settings

    symbols = symbols or load_top50()
    settings = get_settings()
    if settings.mock_llm or not settings.anthropic_api_key:
        return _mock_ticker_reports("Sentiment", symbols, ["bullish", "neutral", "bearish"])

    market = await gather_market_news()
    per_ticker = await gather_ticker_news(symbols)
    compact: dict[str, list[dict[str, Any]]] = {}
    for s in symbols:
        related = filter_news_for_symbol(market, s)[:5] + per_ticker.get(s, [])[:5]
        compact[s] = [
            {"source": n.get("source"), "title": n.get("title"), "summary": n.get("summary", "")[:200]}
            for n in related
        ]

    def build_user(batch: list[str]) -> str:
        return json.dumps(
            {
                "tickers": batch,
                "macro_headlines": market[:25],
                "per_ticker_news": {s: compact.get(s, []) for s in batch},
                "lessons": lessons or [],
                "instruction": "Produce compact sentiment JSON for every ticker in this batch.",
            },
            default=str,
        )[:35000]

    try:
        result = await _claude_batched("sentiment", symbols, build_user, batch_size=5, max_tokens=2200, timeout=50.0)
        if result.get("mock"):
            return _mock_ticker_reports("Sentiment", symbols, ["bullish", "neutral", "bearish"])
        # Fill any missing symbols heuristically
        have = {str(t.get("symbol")).upper() for t in result.get("tickers") or []}
        for s in symbols:
            if s.upper() not in have:
                bias, score, summary = _bias_from_headlines(compact.get(s) or [])
                result.setdefault("tickers", []).append(
                    {"symbol": s, "summary": summary[:240], "bias": bias, "score": score, "catalysts": [], "sources": []}
                )
        return result
    except Exception as e:
        return _heuristic_sentiment(symbols, market, compact, str(e))


async def run_technicals(symbols: list[str] | None = None, lessons: list[str] | None = None) -> dict[str, Any]:
    from app.config import get_settings

    symbols = symbols or load_top50()
    settings = get_settings()
    if settings.mock_llm or not settings.anthropic_api_key:
        return _mock_ticker_reports("Technicals", symbols, ["neutral", "bullish", "bearish"])

    tech: dict[str, Any] = {}

    async def one(s: str) -> None:
        try:
            short_df, long_df = await asyncio.gather(
                get_bars(s, interval="15m", period="5d"),
                get_bars(s, interval="1d", period="3mo"),
            )
            tech[s] = {
                "short_term": technical_snapshot(short_df),
                "long_term": technical_snapshot(long_df),
            }
        except Exception as e:
            tech[s] = {"error": str(e), "short_term": {"error": "no data"}, "long_term": {"error": "no data"}}

    for i in range(0, len(symbols), 2):
        await asyncio.gather(*[one(s) for s in symbols[i : i + 2]])
        if i + 2 < len(symbols):
            await asyncio.sleep(0.5)

    def build_user(batch: list[str]) -> str:
        slim = {}
        for s in batch:
            pack = tech.get(s) or {}
            st = pack.get("short_term") or {}
            lt = pack.get("long_term") or {}
            slim[s] = {
                "short_term": {k: st.get(k) for k in ("last", "period_return_pct", "volatility", "sma_fast", "sma_slow", "trend", "error") if k in st or st.get("error")},
                "long_term": {k: lt.get(k) for k in ("last", "period_return_pct", "volatility", "sma_fast", "sma_slow", "trend", "error") if k in lt or lt.get("error")},
            }
        return json.dumps(
            {
                "tickers": batch,
                "technicals": slim,
                "lessons": lessons or [],
                "instruction": "Independent TA only. Compact report per ticker. Data gaps → neutral + note.",
            },
            default=str,
        )[:35000]

    try:
        result = await _claude_batched("technicals", symbols, build_user, batch_size=5, max_tokens=2200, timeout=50.0)
        if result.get("mock"):
            return _mock_ticker_reports("Technicals", symbols, ["neutral", "bullish", "bearish"])
        have = {str(t.get("symbol")).upper() for t in result.get("tickers") or []}
        for s in symbols:
            if s.upper() not in have:
                bias, score, summary, long_t, short_t = _bias_from_tech(tech.get(s) or {})
                result.setdefault("tickers", []).append(
                    {"symbol": s, "summary": summary, "bias": bias, "score": score, "long_term": long_t, "short_term": short_t}
                )
        return result
    except Exception as e:
        return _heuristic_technicals(symbols, tech, str(e))


async def run_fundamentals(symbols: list[str] | None = None, lessons: list[str] | None = None) -> dict[str, Any]:
    from app.config import get_settings

    symbols = symbols or load_top50()
    settings = get_settings()
    if settings.mock_llm or not settings.anthropic_api_key:
        return _mock_ticker_reports("Fundamentals", symbols, ["neutral", "bullish", "neutral"])

    funds = await batch_fundamentals(symbols)
    avgs = sector_averages(funds)

    def build_user(batch: list[str]) -> str:
        return json.dumps(
            {
                "tickers": batch,
                "fundamentals": {s: funds.get(s) for s in batch},
                "sector_averages": avgs,
                "lessons": lessons or [],
                "instruction": "Compact fundamental report per company in this batch.",
            },
            default=str,
        )[:35000]

    try:
        result = await _claude_batched("fundamentals", symbols, build_user, batch_size=5, max_tokens=2200, timeout=50.0)
        if result.get("mock"):
            return _mock_ticker_reports("Fundamentals", symbols, ["neutral", "bullish", "neutral"])
        have = {str(t.get("symbol")).upper() for t in result.get("tickers") or []}
        for s in symbols:
            if s.upper() not in have:
                result.setdefault("tickers", []).extend(_heuristic_fundamentals([s], funds, "missing row")["tickers"])
        return result
    except Exception as e:
        return _heuristic_fundamentals(symbols, funds, str(e))


def _compress_for_strategist(report: dict[str, Any], limit: int = 50) -> list[dict[str, Any]]:
    tickers = report.get("tickers") or []
    out = []
    for t in tickers[:limit]:
        out.append(
            {
                "symbol": t.get("symbol"),
                "summary": (t.get("summary") or "")[:400],
                "bias": t.get("bias"),
                "score": t.get("score"),
                "long_term": t.get("long_term"),
                "short_term": t.get("short_term"),
                "events": t.get("events") or t.get("catalysts") or [],
            }
        )
    return out


def _score_blend(sentiment: dict[str, Any], technicals: dict[str, Any], fundamentals: dict[str, Any]) -> list[dict[str, Any]]:
    """Rank names from whatever research survived."""
    by: dict[str, dict[str, Any]] = {}

    def ingest(pack: dict[str, Any], weight: float) -> None:
        for t in pack.get("tickers") or []:
            sym = str(t.get("symbol") or "").upper()
            if not sym:
                continue
            row = by.setdefault(sym, {"symbol": sym, "score": 0.0, "bias_votes": [], "reasons": []})
            bias = (t.get("bias") or "neutral").lower()
            sc = float(t.get("score") or 50)
            signed = sc if bias == "bullish" else (-sc if bias == "bearish" else 0.0)
            row["score"] += signed * weight
            row["bias_votes"].append(bias)
            if t.get("summary"):
                row["reasons"].append(str(t["summary"])[:120])

    ingest(technicals, 1.0)
    ingest(sentiment, 1.0)
    ingest(fundamentals, 0.35)
    ranked = sorted(by.values(), key=lambda r: abs(r["score"]), reverse=True)
    return ranked


def _fallback_day_plan(
    sentiment: dict[str, Any],
    technicals: dict[str, Any],
    fundamentals: dict[str, Any],
    max_pct: float,
    max_names: int,
    operator_brief: ParsedRunBrief | None,
) -> DayPlan:
    timing = "intraday"
    if operator_brief and operator_brief.force_entry_timing:
        timing = operator_brief.force_entry_timing
    elif operator_brief and operator_brief.start_mode == "immediate":
        timing = "intraday"

    ranked = _score_blend(sentiment, technicals, fundamentals)
    trades: list[TradeIntent] = []
    for row in ranked:
        if len(trades) >= min(max_names, 5):
            break
        if abs(row["score"]) < 8:
            continue
        side = Side.LONG if row["score"] >= 0 else Side.SHORT
        # Prefer clear directional majority
        bulls = sum(1 for b in row["bias_votes"] if b == "bullish")
        bears = sum(1 for b in row["bias_votes"] if b == "bearish")
        if bulls == bears and abs(row["score"]) < 25:
            continue
        if bears > bulls:
            side = Side.SHORT
        elif bulls > bears:
            side = Side.LONG
        pct = min(max_pct, 0.12 if abs(row["score"]) < 40 else 0.18)
        reason = row["reasons"][0] if row["reasons"] else f"Fallback blend score={row['score']:.0f}"
        trades.append(
            TradeIntent(
                symbol=row["symbol"],
                side=side,
                allocation_pct=pct,
                entry_timing=timing,  # type: ignore[arg-type]
                reason=f"[desk fallback] {reason}",
                stop_loss_pct=0.015,
                take_profit_pct=0.02,
            )
        )

    # Absolute last resort for simulate / trade-now: take top |score| names even if weak
    force = bool(
        operator_brief
        and (
            operator_brief.simulate_after_hours
            or operator_brief.trade_immediately_after_research
            or operator_brief.start_mode == "immediate"
        )
    )
    if not trades and force and ranked:
        for row in ranked[: min(3, max_names)]:
            side = Side.LONG if row["score"] >= 0 else Side.SHORT
            trades.append(
                TradeIntent(
                    symbol=row["symbol"],
                    side=side,
                    allocation_pct=min(0.10, max_pct),
                    entry_timing=timing,  # type: ignore[arg-type]
                    reason="[desk fallback] Partial research — forced paper/sim opens from best available bias.",
                    stop_loss_pct=0.015,
                    take_profit_pct=0.02,
                )
            )

    narrative = (
        "Engine fallback plan: research packs were partial/empty for Claude synthesis; "
        "trades ranked from surviving Sentiment/Technicals/Fundamentals scores."
        if trades
        else "No usable research edges after fallback — book stays flat."
    )
    return DayPlan(trades=trades, narrative=narrative, priorities=[t.symbol for t in trades])


async def run_strategist(
    sentiment: dict[str, Any],
    technicals: dict[str, Any],
    fundamentals: dict[str, Any],
    capital: float,
    max_pct: float,
    max_names: int,
    lessons: list[str] | None = None,
    operator_brief: ParsedRunBrief | None = None,
) -> DayPlan:
    force = bool(
        operator_brief
        and (
            operator_brief.simulate_after_hours
            or operator_brief.trade_immediately_after_research
            or operator_brief.start_mode == "immediate"
        )
    )
    user = json.dumps(
        {
            "capital_usd": capital,
            "max_position_pct": max_pct,
            "max_positions": max_names,
            "priority": {"Sentiment": 1, "Technicals": 1, "Fundamentals": 3},
            "operator_brief": operator_brief.model_dump(mode="json") if operator_brief else None,
            "research_status": {
                "sentiment_n": len(sentiment.get("tickers") or []),
                "technicals_n": len(technicals.get("tickers") or []),
                "fundamentals_n": len(fundamentals.get("tickers") or []),
                "sentiment_error": sentiment.get("error") or sentiment.get("degraded"),
                "technicals_error": technicals.get("error") or technicals.get("degraded"),
                "fundamentals_error": fundamentals.get("error") or fundamentals.get("degraded"),
            },
            "sentiment": {"macro": sentiment.get("macro"), "tickers": _compress_for_strategist(sentiment)},
            "technicals": {"tickers": _compress_for_strategist(technicals)},
            "fundamentals": {"tickers": _compress_for_strategist(fundamentals)},
            "lessons": lessons or [],
            "instruction": (
                "Propose 2–5 trades from available research. "
                "Partial packs are OK — do NOT return empty trades[] if any ticker biases exist. "
                + ("Operator wants simulate/immediate execution." if force else "")
            ),
        },
        default=str,
    )
    try:
        result = await claude_json(load_prompt("strategist"), user, max_tokens=2500, timeout=60.0)
    except Exception:
        return _fallback_day_plan(sentiment, technicals, fundamentals, max_pct, max_names, operator_brief)

    if result.get("mock"):
        # Deterministic mock plan: top scored bullish from technicals/sentiment blend
        timing = "intraday"
        if operator_brief and operator_brief.force_entry_timing:
            timing = operator_brief.force_entry_timing
        elif operator_brief and operator_brief.start_mode == "immediate":
            timing = "intraday"
        trades = [
            TradeIntent(
                symbol="AAPL",
                side=Side.LONG,
                allocation_pct=min(0.15, max_pct),
                entry_timing=timing,  # type: ignore[arg-type]
                reason="Mock: strong mega-cap liquidity and constructive tape.",
                stop_loss_pct=0.015,
                take_profit_pct=0.02,
            ),
            TradeIntent(
                symbol="MSFT",
                side=Side.LONG,
                allocation_pct=min(0.12, max_pct),
                entry_timing="wait" if timing == "at_open" else timing,  # type: ignore[arg-type]
                reason="Mock: secondary megacap; follow operator brief.",
                stop_loss_pct=0.015,
                take_profit_pct=0.02,
            ),
            TradeIntent(
                symbol="NVDA",
                side=Side.SHORT,
                allocation_pct=min(0.10, max_pct),
                entry_timing=timing,  # type: ignore[arg-type]
                reason="Mock: fade extended short-term strength (paper short).",
                stop_loss_pct=0.02,
                take_profit_pct=0.025,
            ),
        ][:max_names]
        return DayPlan(
            trades=trades,
            narrative=(operator_brief.strategy_notes if operator_brief else "Mock day plan for local demo.")[:500],
            priorities=["AAPL", "MSFT", "NVDA"],
        )

    trades: list[TradeIntent] = []
    for t in (result.get("trades") or [])[:max_names]:
        try:
            pct = float(t.get("allocation_pct") or 0)
            pct = min(pct, max_pct)
            if pct <= 0:
                continue
            timing = t.get("entry_timing") or "at_open"
            if operator_brief and operator_brief.force_entry_timing:
                timing = operator_brief.force_entry_timing
            trades.append(
                TradeIntent(
                    symbol=str(t["symbol"]).upper(),
                    side=Side(str(t.get("side", "long")).lower()),
                    allocation_pct=pct,
                    entry_timing=timing,
                    reason=t.get("reason") or "",
                    stop_loss_pct=t.get("stop_loss_pct"),
                    take_profit_pct=t.get("take_profit_pct"),
                )
            )
        except Exception:
            continue
    # Enforce total sanity: if sum > 1, scale down
    total = sum(x.allocation_pct for x in trades)
    if total > 1 and total > 0:
        for i, tr in enumerate(trades):
            trades[i] = tr.model_copy(update={"allocation_pct": tr.allocation_pct / total})

    if not trades:
        return _fallback_day_plan(sentiment, technicals, fundamentals, max_pct, max_names, operator_brief)

    return DayPlan(
        trades=trades,
        narrative=result.get("narrative") or "",
        priorities=result.get("priorities") or [t.symbol for t in trades],
    )


async def committee_vote(
    trade: TradeIntent | None,
    action: str,
    context: dict[str, Any],
) -> CommitteeResult:
    """Strategist, Risk, Desk majority vote."""
    payload = json.dumps(
        {"proposed_action": action, "trade": trade.model_dump() if trade else None, "context": context},
        default=str,
    )

    async def vote(name: str) -> VoteDecision:
        prompt_map = {
            "Strategist": "strategist",
            "Risk": "risk",
            "Desk": "desk",
        }
        ballot = (
            f"You are {name}. Vote on this trading action. "
            f'Return JSON only: {{"voter":"{name}","action":"approve|reject|modify|close|hold",'
            f'"reason":"...","allocation_pct":null,"symbol":null,"side":null}}'
        )
        result = await claude_json(
            load_prompt(prompt_map[name]) + "\n\n" + ballot,
            payload,
            max_tokens=1024,
        )
        if result.get("mock"):
            act = "approve"
            return VoteDecision(
                action=act,
                symbol=trade.symbol if trade else None,
                side=trade.side if trade else None,
                reason=f"Mock {name} approves",
                voter=name,
            )
        return VoteDecision(
            action=result.get("action") or "hold",
            symbol=result.get("symbol") or (trade.symbol if trade else None),
            side=Side(result["side"]) if result.get("side") else (trade.side if trade else None),
            allocation_pct=result.get("allocation_pct"),
            reason=result.get("reason") or "",
            voter=name,
        )

    votes = [await vote("Strategist"), await vote("Risk"), await vote("Desk")]
    approve_like = {"approve", "close", "modify"}
    yes = sum(1 for v in votes if v.action in approve_like)
    approved = yes >= 2
    final = action if approved else "reject"
    final_trade = trade
    if approved and trade:
        for v in votes:
            if v.action == "modify" and v.allocation_pct:
                final_trade = trade.model_copy(
                    update={"allocation_pct": min(v.allocation_pct, trade.allocation_pct)}
                )
                break
    return CommitteeResult(
        approved=approved,
        votes=votes,
        final_action=final,
        trade=final_trade,
        reason="; ".join(f"{v.voter}:{v.action}" for v in votes),
    )


async def run_risk_check(
    portfolio: dict[str, Any],
    plan: DayPlan | None,
    live_candles: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = await claude_json(
        load_prompt("risk"),
        json.dumps(
            {
                "portfolio": portfolio,
                "plan": plan.model_dump() if plan else None,
                "live_candles": live_candles or {},
                "data_note": "Candles from yfinance are typically ~15m delayed for US equities. Use for structure/risk, not tick scalping.",
            },
            default=str,
        ),
        max_tokens=2048,
    )
    if result.get("mock"):
        actions = []
        for p in portfolio.get("positions") or []:
            pnl_pct = p.get("unrealized_pnl_pct") or 0
            if pnl_pct <= -1.5:
                actions.append({"type": "close", "symbol": p["symbol"], "reason": f"Mock SL trigger {pnl_pct:.2f}%"})
            elif pnl_pct >= 2.0:
                actions.append({"type": "close", "symbol": p["symbol"], "reason": f"Mock TP trigger {pnl_pct:.2f}%"})
        return {"agent": "Risk", "actions": actions, "notes": "mock risk scan (candles available but delayed)"}
    return result


async def run_desk_eod(
    day_context: dict[str, Any],
) -> dict[str, Any]:
    result = await claude_json(
        load_prompt("desk"),
        json.dumps(
            {
                "instruction": "Produce EOD scores for Sentiment,Technicals,Fundamentals,Strategist,Executor,Book,Risk,Desk plus pros/cons/mistakes/conclusions/lessons.",
                "day": day_context,
            },
            default=str,
        ),
        max_tokens=4096,
    )
    if result.get("mock"):
        agents = ["Sentiment", "Technicals", "Fundamentals", "Strategist", "Executor", "Book", "Risk", "Desk", "Floor"]
        return {
            "agent_scores": {
                a: {"score": 70 + (i % 20), "feedback": f"Mock feedback for {a}: solid process.", "useful": True, "correct": True}
                for i, a in enumerate(agents)
            },
            "pros": ["Followed risk caps", "Flattened at close"],
            "cons": ["Limited live news depth in mock mode"],
            "mistakes": ["None critical in mock run"],
            "conclusions": "Mock day completed under risk constraints.",
            "lessons": [
                "Keep max 30% per name.",
                "Re-check earnings calendar before holding through open.",
            ],
        }
    return result


async def ask_floor(question: str, desk_context: dict[str, Any]) -> str:
    """Commentator only — does not mutate trading state."""
    from app.agents.llm import claude_text

    # Keep context lean so commentary stays fast while the desk runs
    lean = {
        "session": desk_context.get("session"),
        "portfolio": {
            "day_pnl": (desk_context.get("portfolio") or {}).get("day_pnl"),
            "positions": (desk_context.get("portfolio") or {}).get("positions") or [],
        },
        "recent_messages": (desk_context.get("recent_messages") or [])[-12:],
        "recent_trades": (desk_context.get("recent_trades") or [])[:10],
        "budget": desk_context.get("budget"),
    }
    try:
        answer = await claude_text(
            load_prompt("floor"),
            json.dumps({"operator_question": question, "desk_context": lean}, default=str)[:40000],
            max_tokens=1024,
            timeout=45.0,
        )
    except Exception as e:
        return f"Floor temporarily unavailable ({e}). Desk is still running."
    if answer.startswith("[mock]"):
        phase = (desk_context.get("session") or {}).get("phase") or "idle"
        npos = len((desk_context.get("portfolio") or {}).get("positions") or [])
        return (
            f"Floor (mock): Floor is in phase «{phase}» with {npos} open position(s). "
            f"You asked: {question[:240]}. Wire Anthropic for a full commentary."
        )
    return answer
