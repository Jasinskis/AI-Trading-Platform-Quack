from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.schemas.models import PortfolioSnapshot, PositionView, Side


@dataclass
class OrderResult:
    ok: bool
    broker_ref: str
    symbol: str
    side: Side
    qty: float
    price: float
    source: str
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class Broker(ABC):
    name: str = "base"

    @abstractmethod
    async def connect(self) -> bool: ...

    @abstractmethod
    async def get_portfolio(self, capital_cap: float | None = None) -> PortfolioSnapshot: ...

    @abstractmethod
    async def market_order(
        self,
        symbol: str,
        side: Side,
        notional_usd: float,
        reason: str = "",
    ) -> OrderResult: ...

    @abstractmethod
    async def close_position(self, symbol: str, side: Side | None = None) -> OrderResult: ...

    @abstractmethod
    async def flatten_all(self) -> list[OrderResult]: ...

    async def ping(self) -> bool:
        return await self.connect()


class MockBroker(Broker):
    """In-memory paper broker for local demo without T212 credentials."""

    name = "mock"

    def __init__(self, starting_cash: float = 1000.0) -> None:
        self.cash = starting_cash
        self.starting_cash = starting_cash
        self.positions: dict[str, PositionView] = {}
        self._prices: dict[str, float] = {}
        self._order_id = 0

    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price
        if symbol in self.positions:
            p = self.positions[symbol]
            last = price
            mv = abs(p.qty) * last
            if p.side == Side.LONG:
                pnl = (last - p.avg_price) * p.qty
            else:
                pnl = (p.avg_price - last) * abs(p.qty)
            pnl_pct = (pnl / (p.avg_price * abs(p.qty))) * 100 if p.avg_price and p.qty else 0
            self.positions[symbol] = p.model_copy(
                update={
                    "last_price": last,
                    "market_value": mv,
                    "unrealized_pnl": pnl,
                    "unrealized_pnl_pct": pnl_pct,
                }
            )

    async def connect(self) -> bool:
        return True

    async def get_portfolio(self, capital_cap: float | None = None) -> PortfolioSnapshot:
        positions = list(self.positions.values())
        used = sum(p.market_value for p in positions)
        equity = self.cash + used
        day_pnl = sum(p.unrealized_pnl for p in positions)
        total = capital_cap or self.starting_cash
        for p in positions:
            p.allocation_pct = (p.market_value / total) if total else 0
        return PortfolioSnapshot(
            cash_used=used,
            cash_unused=self.cash,
            total_equity=equity,
            day_pnl=day_pnl,
            day_pnl_pct=(day_pnl / total * 100) if total else 0,
            positions=positions,
        )

    async def market_order(
        self,
        symbol: str,
        side: Side,
        notional_usd: float,
        reason: str = "",
    ) -> OrderResult:
        from app.data.prices import get_last_price

        price = self._prices.get(symbol)
        if not price:
            try:
                price = await get_last_price(symbol)
            except Exception:
                price = None
        if not price or price <= 0:
            return OrderResult(
                False,
                "",
                symbol,
                side,
                0,
                0.0,
                self.name,
                f"no real market price for {symbol} — refusing fake fill",
            )
        self._prices[symbol] = price
        qty = notional_usd / price if price else 0
        if qty <= 0:
            return OrderResult(False, "", symbol, side, 0, price, self.name, "zero qty")

        self._order_id += 1
        ref = f"mock-{self._order_id}"

        if side == Side.LONG:
            if notional_usd > self.cash + 1e-6:
                return OrderResult(False, "", symbol, side, 0, price, self.name, "insufficient cash")
            self.cash -= notional_usd
            existing = self.positions.get(symbol)
            if existing and existing.side == Side.LONG:
                new_qty = existing.qty + qty
                avg = ((existing.avg_price * existing.qty) + (price * qty)) / new_qty
                self.positions[symbol] = existing.model_copy(
                    update={"qty": new_qty, "avg_price": avg, "last_price": price, "reason": reason}
                )
            else:
                self.positions[symbol] = PositionView(
                    symbol=symbol,
                    side=Side.LONG,
                    qty=qty,
                    avg_price=price,
                    last_price=price,
                    market_value=notional_usd,
                    unrealized_pnl=0,
                    unrealized_pnl_pct=0,
                    allocation_pct=0,
                    source="mock",
                    opened_at=datetime.now(timezone.utc),
                    reason=reason,
                )
        else:
            # Paper short: reserve notional from cash as margin proxy
            if notional_usd > self.cash + 1e-6:
                return OrderResult(False, "", symbol, side, 0, price, self.name, "insufficient cash for short")
            self.cash -= notional_usd  # collateral lock
            self.positions[symbol] = PositionView(
                symbol=symbol,
                side=Side.SHORT,
                qty=-qty,
                avg_price=price,
                last_price=price,
                market_value=notional_usd,
                unrealized_pnl=0,
                unrealized_pnl_pct=0,
                allocation_pct=0,
                source="paper_short",
                opened_at=datetime.now(timezone.utc),
                reason=reason,
            )

        return OrderResult(True, ref, symbol, side, qty if side == Side.LONG else -qty, price, self.name, "filled")

    async def close_position(self, symbol: str, side: Side | None = None) -> OrderResult:
        p = self.positions.get(symbol)
        if not p:
            return OrderResult(False, "", symbol, side or Side.LONG, 0, 0, self.name, "no position")
        from app.data.prices import get_last_price

        price = self._prices.get(symbol) or await get_last_price(symbol) or p.last_price
        if p.side == Side.LONG:
            proceeds = abs(p.qty) * price
            self.cash += proceeds
        else:
            # Release collateral + PnL
            collateral = abs(p.qty) * p.avg_price
            pnl = (p.avg_price - price) * abs(p.qty)
            self.cash += collateral + pnl
        del self.positions[symbol]
        self._order_id += 1
        return OrderResult(
            True,
            f"mock-close-{self._order_id}",
            symbol,
            p.side,
            -p.qty,
            price,
            self.name,
            "closed",
        )

    async def flatten_all(self) -> list[OrderResult]:
        results = []
        for symbol in list(self.positions.keys()):
            results.append(await self.close_position(symbol))
        return results
