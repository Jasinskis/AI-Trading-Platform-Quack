from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import get_settings


class Base(DeclarativeBase):
    pass


class SessionRow(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(32), default="idle")
    phase: Mapped[str] = mapped_column(String(64), default="idle")
    capital_usd: Mapped[float] = mapped_column(Float)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    max_position_pct: Mapped[float] = mapped_column(Float, default=0.30)
    max_positions_per_day: Mapped[int] = mapped_column(Integer, default=10)
    flatten_at_close: Mapped[bool] = mapped_column(Boolean, default=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class MessageRow(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    from_agent: Mapped[str] = mapped_column(String(64))
    to_agent: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), default="chat")
    content: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class TradeRow(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, index=True)
    trade_date: Mapped[str] = mapped_column(String(16), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    qty: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    allocation_pct: Mapped[float] = mapped_column(Float, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    broker_ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="mock")
    status: Mapped[str] = mapped_column(String(32), default="filled")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PositionRow(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    qty: Mapped[float] = mapped_column(Float)
    avg_price: Mapped[float] = mapped_column(Float)
    allocation_pct: Mapped[float] = mapped_column(Float, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(32), default="mock")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ReportRow(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, index=True)
    trade_date: Mapped[str] = mapped_column(String(16), index=True)
    agent: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(256), default="")
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LibraryRow(Base):
    __tablename__ = "library"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, index=True)
    trade_date: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    total_pnl: Mapped[float] = mapped_column(Float, default=0)
    total_pnl_pct: Mapped[float] = mapped_column(Float, default=0)
    best_symbol: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    worst_symbol: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    pros: Mapped[list[Any]] = mapped_column(JSON, default=list)
    cons: Mapped[list[Any]] = mapped_column(JSON, default=list)
    mistakes: Mapped[list[Any]] = mapped_column(JSON, default=list)
    conclusions: Mapped[str] = mapped_column(Text, default="")
    agent_scores: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    full_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LessonRow(Base):
    __tablename__ = "lessons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[str] = mapped_column(String(16), index=True)
    agent: Mapped[str] = mapped_column(String(64), default="system")
    lesson: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SnapshotRow(Base):
    """Time-series book snapshots for equity / return / drawdown / cash charts."""

    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    equity: Mapped[float] = mapped_column(Float, default=0)
    cash_used: Mapped[float] = mapped_column(Float, default=0)
    cash_unused: Mapped[float] = mapped_column(Float, default=0)
    day_pnl: Mapped[float] = mapped_column(Float, default=0)
    day_pnl_pct: Mapped[float] = mapped_column(Float, default=0)
    positions: Mapped[list[Any]] = mapped_column(JSON, default=list)


settings = get_settings()
engine = create_async_engine(settings.database_url, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    async with SessionLocal() as session:
        yield session
