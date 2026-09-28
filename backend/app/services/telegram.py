from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from app.config import get_settings
from app.services.cost import cost_tracker

# Desk log kinds mirrored to Telegram
TELEGRAM_LOG_KINDS = {
    "brief",
    "prompt",
    "plan",
    "pause",
    "eod",
    "budget",
    "error",
    "trade",
    "vote",
}

_send_lock = asyncio.Lock()
_last_sent_at: float = 0.0
_MIN_GAP_SEC = 0.25
_bg_tasks: set[asyncio.Task] = set()
_client: httpx.AsyncClient | None = None


def resolve_chat_id() -> str:
    """Prefer .env TELEGRAM_CHAT_ID, then discovered/persisted id."""
    s = get_settings()
    if s.telegram_chat_id:
        return str(s.telegram_chat_id).strip()
    return (cost_tracker.get_telegram_chat_id() or "").strip()


async def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=8.0)
    return _client


async def discover_telegram_chat_id() -> str | None:
    existing = resolve_chat_id()
    if existing:
        cost_tracker.set_telegram_chat_id(existing)
        return existing
    s = get_settings()
    if not s.telegram_bot_token:
        return None
    url = f"https://api.telegram.org/bot{s.telegram_bot_token}/getUpdates"
    try:
        client = await _http()
        r = await client.get(url)
        data = r.json()
        for upd in reversed(data.get("result") or []):
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = msg.get("chat") or {}
            if chat.get("id") is not None:
                cid = str(chat["id"])
                cost_tracker.set_telegram_chat_id(cid)
                return cid
    except Exception:
        return None
    return None


async def send_telegram(text: str) -> bool:
    s = get_settings()
    if not s.telegram_bot_token:
        return False
    chat_id = resolve_chat_id()
    if not chat_id:
        chat_id = await discover_telegram_chat_id() or ""
    if not chat_id:
        return False
    url = f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage"
    global _last_sent_at
    async with _send_lock:
        now = datetime.now(timezone.utc).timestamp()
        gap = _MIN_GAP_SEC - (now - _last_sent_at)
        if gap > 0:
            await asyncio.sleep(gap)
        try:
            client = await _http()
            r = await client.post(
                url,
                json={"chat_id": chat_id, "text": text[:3500], "disable_web_page_preview": True},
            )
            _last_sent_at = datetime.now(timezone.utc).timestamp()
            if r.status_code == 429:
                await asyncio.sleep(1.0)
                r = await client.post(
                    url,
                    json={"chat_id": chat_id, "text": text[:3500], "disable_web_page_preview": True},
                )
                _last_sent_at = datetime.now(timezone.utc).timestamp()
            return r.status_code == 200 and bool(r.json().get("ok"))
        except Exception:
            return False


def send_telegram_bg(text: str) -> None:
    """Queue Telegram send without blocking the desk."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _run() -> None:
        try:
            await send_telegram(text)
        except Exception:
            pass

    task = loop.create_task(_run())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def notify_desk(from_agent: str, content: str, *, kind: str = "chat") -> bool:
    if kind not in TELEGRAM_LOG_KINDS:
        return False
    send_telegram_bg(f"[{kind.upper()}] {from_agent}\n{content[:3200]}")
    return True


def format_trade_alert(
    *,
    ts: str,
    symbol: str,
    side: str,
    allocation_pct: float,
    reason: str,
    simulated: bool = False,
) -> str:
    prefix = "SIMULATED TRADE (after-hours / not T212)\n" if simulated else "TRADE "
    return (
        f"{prefix}{ts}\n"
        f"{side.upper()} {symbol}\n"
        f"Allocation: {allocation_pct*100:.1f}% of book\n"
        f"Why: {reason}"
        + (
            "\nNote: paper fill on yfinance marks — not a live Trading212 market execution."
            if simulated
            else ""
        )
    )


def format_eod_summary(summary: dict) -> str:
    scores = summary.get("agent_scores") or {}
    score_lines = []
    for agent, data in scores.items():
        if isinstance(data, dict):
            score_lines.append(f"  {agent}: {data.get('score')} — {data.get('feedback','')[:120]}")
        else:
            score_lines.append(f"  {agent}: {data}")
    simulated = bool(summary.get("simulated_after_hours")) or (
        (summary.get("full_report") or {}).get("simulated_after_hours")
        if isinstance(summary.get("full_report"), dict)
        else False
    )
    note = summary.get("execution_note") or (
        (summary.get("full_report") or {}).get("execution_note")
        if isinstance(summary.get("full_report"), dict)
        else ""
    )
    header = (
        f"EOD SUMMARY {summary.get('trade_date')} — AFTER-HOURS SIMULATION (NOT T212)\n"
        f"{note or 'Simulated paper broker; fills were not live Trading212 executions.'}\n"
        if simulated
        else f"EOD SUMMARY {summary.get('trade_date')}\n"
    )
    return (
        header
        + f"PnL: ${summary.get('total_pnl', 0):.2f} ({summary.get('total_pnl_pct', 0):.2f}%)\n"
        f"Best: {summary.get('best_symbol')} | Worst: {summary.get('worst_symbol')}\n"
        f"Conclusions: {summary.get('conclusions','')[:500]}\n"
        f"Mistakes: {', '.join(summary.get('mistakes') or [])}\n"
        f"Agent scores:\n" + "\n".join(score_lines)
    )


def format_budget_pause(snap: dict) -> str:
    return (
        "QUACK PAUSED — Anthropic budget limit reached\n"
        f"Spent: ${snap.get('spent_usd', 0):.4f} / ${snap.get('budget_usd', 10):.2f}\n"
        f"Tokens in/out: {snap.get('input_tokens')} / {snap.get('output_tokens')}\n"
        f"Calls: {snap.get('calls')}\n"
        "All agent operations are stopped until you raise the limit or reset the budget."
    )


def format_phase(phase: str, status: str) -> str:
    return f"PHASE → {phase}\nStatus: {status}"
