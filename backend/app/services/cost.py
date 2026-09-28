from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from app.config import ROOT, get_settings

STATE_PATH = ROOT / "quack_runtime_state.json"

Broadcast = Callable[[dict[str, Any]], Awaitable[None]]


class BudgetExceeded(Exception):
    def __init__(self, spent: float, limit: float):
        self.spent = spent
        self.limit = limit
        super().__init__(f"Anthropic budget exceeded: ${spent:.4f} / ${limit:.2f}")


@dataclass
class CostState:
    spent_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    paused: bool = False
    pause_reason: str = ""
    telegram_chat_id: str = ""
    updated_at: str = ""


class CostTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._broadcasts: list[Broadcast] = []
        self.state = self._load()

    def subscribe(self, cb: Broadcast) -> None:
        self._broadcasts.append(cb)

    def _load(self) -> CostState:
        if STATE_PATH.exists():
            try:
                data = json.loads(STATE_PATH.read_text())
                base = CostState()
                return CostState(
                    spent_usd=float(data.get("spent_usd", 0) or 0),
                    input_tokens=int(data.get("input_tokens", 0) or 0),
                    output_tokens=int(data.get("output_tokens", 0) or 0),
                    calls=int(data.get("calls", 0) or 0),
                    paused=bool(data.get("paused", False)),
                    pause_reason=str(data.get("pause_reason") or ""),
                    telegram_chat_id=str(data.get("telegram_chat_id") or ""),
                    updated_at=str(data.get("updated_at") or ""),
                )
            except Exception:
                pass
        return CostState()

    def _save(self) -> None:
        self.state.updated_at = datetime.now(timezone.utc).isoformat()
        STATE_PATH.write_text(json.dumps(asdict(self.state), indent=2))

    def snapshot(self) -> dict[str, Any]:
        s = get_settings()
        limit = float(getattr(s, "anthropic_budget_usd", 10.0) or 10.0)
        with self._lock:
            spent = self.state.spent_usd
            input_tokens = self.state.input_tokens
            output_tokens = self.state.output_tokens
            calls = self.state.calls
            paused = self.state.paused
            pause_reason = self.state.pause_reason
        return {
            "spent_usd": round(spent, 6),
            "budget_usd": limit,
            "remaining_usd": round(max(0.0, limit - spent), 6),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "calls": calls,
            "paused": paused,
            "pause_reason": pause_reason,
            "pct_used": round(min(100.0, (spent / limit) * 100), 2) if limit else 100.0,
        }

    def ensure_allowed(self) -> None:
        s = get_settings()
        limit = float(getattr(s, "anthropic_budget_usd", 10.0) or 10.0)
        with self._lock:
            if self.state.paused or self.state.spent_usd >= limit:
                self.state.paused = True
                if not self.state.pause_reason:
                    self.state.pause_reason = f"Anthropic spend reached ${self.state.spent_usd:.2f} (limit ${limit:.2f}). Operations paused."
                self._save()
                spent = self.state.spent_usd
            else:
                return
        raise BudgetExceeded(spent, limit)

    async def record_usage(self, input_tokens: int, output_tokens: int) -> dict[str, Any]:
        s = get_settings()
        limit = float(getattr(s, "anthropic_budget_usd", 10.0) or 10.0)
        in_rate = float(getattr(s, "anthropic_input_usd_per_mtok", 3.0) or 3.0)
        out_rate = float(getattr(s, "anthropic_output_usd_per_mtok", 15.0) or 15.0)
        cost = (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate

        just_exceeded = False
        pause_reason = ""
        with self._lock:
            self.state.input_tokens += int(input_tokens or 0)
            self.state.output_tokens += int(output_tokens or 0)
            self.state.calls += 1
            self.state.spent_usd += cost
            if self.state.spent_usd >= limit and not self.state.paused:
                self.state.paused = True
                self.state.pause_reason = (
                    f"Anthropic token spend reached ${self.state.spent_usd:.2f} "
                    f"(limit ${limit:.2f}). All agent operations paused."
                )
                just_exceeded = True
                pause_reason = self.state.pause_reason
            self._save()

        snap = self.snapshot()

        for cb in list(self._broadcasts):
            try:
                await cb({"type": "budget", "data": snap})
            except Exception:
                pass

        if just_exceeded:
            for cb in list(self._broadcasts):
                try:
                    await cb(
                        {
                            "type": "budget_exceeded",
                            "message": pause_reason,
                            "data": snap,
                        }
                    )
                except Exception:
                    pass
            raise BudgetExceeded(snap["spent_usd"], limit)

        return snap

    def set_telegram_chat_id(self, chat_id: str) -> None:
        with self._lock:
            self.state.telegram_chat_id = str(chat_id)
            self._save()

    def get_telegram_chat_id(self) -> str:
        s = get_settings()
        if s.telegram_chat_id:
            return s.telegram_chat_id
        return self.state.telegram_chat_id

    def reset_budget(self) -> None:
        with self._lock:
            self.state.spent_usd = 0.0
            self.state.input_tokens = 0
            self.state.output_tokens = 0
            self.state.calls = 0
            self.state.paused = False
            self.state.pause_reason = ""
            self._save()


cost_tracker = CostTracker()
