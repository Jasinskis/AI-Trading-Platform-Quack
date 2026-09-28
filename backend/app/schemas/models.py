from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class SessionStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PREOPEN = "preopen"
    OPEN = "open"
    PAUSED = "paused"
    CLOSING = "closing"
    STOPPED = "stopped"
    ERROR = "error"


class StartSessionRequest(BaseModel):
    """Primary input is a freeform operator prompt. Structured fields are optional overrides."""

    prompt: str = Field(min_length=3, description="Natural-language mission brief for the desk")
    capital_usd: Optional[float] = Field(default=None, gt=0)
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    max_position_pct: Optional[float] = Field(default=None, gt=0, le=1)
    max_positions_per_day: Optional[int] = Field(default=None, ge=1, le=50)
    flatten_at_close: Optional[bool] = None


class ParsedRunBrief(BaseModel):
    capital_usd: float = 1000.0
    start_mode: Literal["immediate", "wait_next_open", "scheduled"] = "immediate"
    start_at: Optional[datetime] = None
    end_mode: Literal["until_close_today", "until_close_each_day", "scheduled", "until_datetime"] = "until_close_today"
    end_at: Optional[datetime] = None
    flatten_at_close: bool = True
    max_position_pct: float = 0.30
    max_positions_per_day: int = 10
    skip_full_preopen: bool = False
    trade_immediately_after_research: bool = True
    force_entry_timing: Optional[Literal["intraday", "at_open"]] = None
    simulate_after_hours: bool = False
    objectives: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    strategy_notes: str = ""
    summary_for_desk: str = ""
    raw_prompt: str = ""


class FloorChatRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: Optional[int] = None



class TradeIntent(BaseModel):
    symbol: str
    side: Side
    allocation_pct: float = Field(gt=0, le=0.30)
    entry_timing: Literal["at_open", "wait", "intraday"] = "at_open"
    reason: str
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None


class DayPlan(BaseModel):
    trades: list[TradeIntent] = Field(default_factory=list)
    narrative: str = ""
    priorities: list[str] = Field(default_factory=list)


class VoteDecision(BaseModel):
    action: Literal["approve", "reject", "modify", "close", "hold"]
    symbol: Optional[str] = None
    side: Optional[Side] = None
    allocation_pct: Optional[float] = None
    reason: str
    voter: str


class CommitteeResult(BaseModel):
    approved: bool
    votes: list[VoteDecision]
    final_action: str
    trade: Optional[TradeIntent] = None
    reason: str


class PositionView(BaseModel):
    symbol: str
    side: Side
    qty: float
    avg_price: float
    last_price: float
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    allocation_pct: float
    source: Literal["t212", "paper_short", "mock"] = "mock"
    opened_at: Optional[datetime] = None
    reason: str = ""


class PortfolioSnapshot(BaseModel):
    cash_used: float
    cash_unused: float
    total_equity: float
    day_pnl: float
    day_pnl_pct: float
    positions: list[PositionView] = Field(default_factory=list)


class AgentMessage(BaseModel):
    id: Optional[int] = None
    session_id: int
    ts: datetime
    from_agent: str
    to_agent: Optional[str] = None
    kind: str = "chat"
    content: str
    payload: dict[str, Any] = Field(default_factory=dict)


class SessionView(BaseModel):
    id: int
    status: SessionStatus
    capital_usd: float
    start_at: datetime
    end_at: datetime
    max_position_pct: float
    max_positions_per_day: int
    flatten_at_close: bool
    created_at: datetime
    phase: str = "idle"
    last_error: Optional[str] = None


class LibraryDaySummary(BaseModel):
    session_id: int
    trade_date: str
    total_pnl: float
    total_pnl_pct: float
    best_symbol: Optional[str] = None
    worst_symbol: Optional[str] = None
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    mistakes: list[str] = Field(default_factory=list)
    conclusions: str = ""
    agent_scores: dict[str, Any] = Field(default_factory=dict)
    full_report: dict[str, Any] = Field(default_factory=dict)


class TickerReportSection(BaseModel):
    symbol: str
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)
    score: Optional[float] = None
    bias: Optional[Literal["bullish", "bearish", "neutral"]] = None


class ResearchBundle(BaseModel):
    agent: str
    generated_at: datetime
    tickers: list[TickerReportSection]
    raw_notes: str = ""
