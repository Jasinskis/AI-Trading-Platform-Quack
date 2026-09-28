from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from app.brokers.base import Broker, OrderResult
from app.config import get_settings
from app.data.prices import get_last_price
from app.schemas.models import PortfolioSnapshot, PositionView, Side


def _to_t212_ticker(symbol: str) -> str:
    """Map Yahoo-style symbol to Trading212 equity ticker."""
    s = symbol.replace(".", "-")  # BRK.B -> BRK-B often; T212 may use BRK.B_US_EQ
    # Common T212 pattern: AAPL_US_EQ
    if s.endswith("_US_EQ"):
        return s
    # BRK.B on T212 is often BRK.B_US_EQ
    return f"{symbol}_US_EQ"


class Trading212Broker(Broker):
    """Trading 212 Demo/Live Invest API. Long-only at broker; shorts via paper ledger."""

    name = "t212"

    def __init__(self) -> None:
        s = get_settings()
        self.base = s.t212_base_url.rstrip("/")
        self.key = s.t212_api_key
        self.secret = s.t212_api_secret
        self._instrument_map: dict[str, str] = {}
        self._paper_shorts: dict[str, PositionView] = {}
        self._short_cash_lock = 0.0
        self._client: Optional[httpx.AsyncClient] = None

    def _auth_header(self) -> str:
        raw = f"{self.key}:{self.secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    async def _client_get(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base,
                headers={"Authorization": self._auth_header()},
                timeout=30.0,
            )
        return self._client

    async def connect(self) -> bool:
        if not self.key or not self.secret:
            return False
        try:
            client = await self._client_get()
            r = await client.get("/equity/account/summary")
            return r.status_code == 200
        except Exception:
            return False

    async def _get(self, path: str) -> Any:
        client = await self._client_get()
        r = await client.get(path)
        if r.status_code == 429:
            await asyncio.sleep(5)
            r = await client.get(path)
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, json: dict) -> Any:
        client = await self._client_get()
        r = await client.post(path, json=json)
        if r.status_code == 429:
            await asyncio.sleep(5)
            r = await client.post(path, json=json)
        if r.status_code >= 400:
            return {"ok": False, "status": r.status_code, "body": r.text}
        return r.json() if r.content else {"ok": True}

    async def resolve_ticker(self, symbol: str) -> str:
        if symbol in self._instrument_map:
            return self._instrument_map[symbol]
        try:
            instruments = await self._get("/equity/metadata/instruments")
            if isinstance(instruments, list):
                for inst in instruments:
                    ticker = inst.get("ticker") or ""
                    short = (inst.get("shortName") or inst.get("name") or "").upper()
                    if ticker.startswith(f"{symbol}_") or short == symbol:
                        self._instrument_map[symbol] = ticker
                        return ticker
        except Exception:
            pass
        t = _to_t212_ticker(symbol)
        self._instrument_map[symbol] = t
        return t

    async def get_portfolio(self, capital_cap: float | None = None) -> PortfolioSnapshot:
        summary = await self._get("/equity/account/summary")
        positions_raw = await self._get("/equity/positions")
        cash_avail = float(summary.get("cash", {}).get("availableToTrade") or 0)
        invested = float(summary.get("investments", {}).get("currentValue") or 0)
        total = float(summary.get("totalValue") or (cash_avail + invested))
        cap = capital_cap or total

        positions: list[PositionView] = []
        if isinstance(positions_raw, list):
            for p in positions_raw:
                ticker = p.get("ticker") or ""
                symbol = ticker.split("_")[0] if ticker else "?"
                qty = float(p.get("quantity") or 0)
                avg = float(p.get("averagePrice") or 0)
                last = float(p.get("currentPrice") or avg)
                mv = abs(qty) * last
                pnl = float(p.get("ppl") or p.get("unrealizedProfitLoss") or (last - avg) * qty)
                positions.append(
                    PositionView(
                        symbol=symbol,
                        side=Side.LONG,
                        qty=qty,
                        avg_price=avg,
                        last_price=last,
                        market_value=mv,
                        unrealized_pnl=pnl,
                        unrealized_pnl_pct=(pnl / (avg * abs(qty)) * 100) if avg and qty else 0,
                        allocation_pct=(mv / cap) if cap else 0,
                        source="t212",
                        reason="",
                    )
                )

        # Merge paper shorts
        for sym, sp in list(self._paper_shorts.items()):
            last = await get_last_price(sym) or sp.last_price
            pnl = (sp.avg_price - last) * abs(sp.qty)
            mv = abs(sp.qty) * last
            positions.append(
                sp.model_copy(
                    update={
                        "last_price": last,
                        "market_value": mv,
                        "unrealized_pnl": pnl,
                        "unrealized_pnl_pct": (pnl / (sp.avg_price * abs(sp.qty)) * 100)
                        if sp.avg_price and sp.qty
                        else 0,
                        "allocation_pct": (mv / cap) if cap else 0,
                    }
                )
            )

        used = sum(p.market_value for p in positions)
        day_pnl = sum(p.unrealized_pnl for p in positions)
        unused = max(0.0, cash_avail - self._short_cash_lock)
        return PortfolioSnapshot(
            cash_used=used,
            cash_unused=unused,
            total_equity=total - self._short_cash_lock + sum(
                (sp.avg_price - (await get_last_price(sym) or sp.last_price)) * abs(sp.qty)
                for sym, sp in self._paper_shorts.items()
            ),
            day_pnl=day_pnl,
            day_pnl_pct=(day_pnl / cap * 100) if cap else 0,
            positions=positions,
        )

    async def market_order(
        self,
        symbol: str,
        side: Side,
        notional_usd: float,
        reason: str = "",
    ) -> OrderResult:
        price = await get_last_price(symbol) or 0
        if price <= 0:
            return OrderResult(False, "", symbol, side, 0, 0, self.name, "no price")

        qty = notional_usd / price

        if side == Side.SHORT:
            # Paper short ledger — T212 Invest cannot short
            self._short_cash_lock += notional_usd
            self._paper_shorts[symbol] = PositionView(
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
            return OrderResult(
                True,
                f"paper-short-{symbol}",
                symbol,
                side,
                -qty,
                price,
                "paper_short",
                "paper short opened (T212 Invest cannot short)",
            )

        t212_ticker = await self.resolve_ticker(symbol)
        # T212 market order uses quantity (shares). Round to 4 decimals.
        share_qty = round(qty, 4)
        if share_qty <= 0:
            return OrderResult(False, "", symbol, side, 0, price, self.name, "qty too small")

        resp = await self._post(
            "/equity/orders/market",
            {"ticker": t212_ticker, "quantity": share_qty},
        )
        if isinstance(resp, dict) and resp.get("ok") is False:
            return OrderResult(
                False,
                "",
                symbol,
                side,
                share_qty,
                price,
                self.name,
                str(resp.get("body") or resp),
                raw=resp,
            )
        ref = str(resp.get("id") or resp.get("orderId") or f"t212-{symbol}")
        return OrderResult(True, ref, symbol, side, share_qty, price, self.name, "submitted", raw=resp or {})

    async def close_position(self, symbol: str, side: Side | None = None) -> OrderResult:
        if symbol in self._paper_shorts and (side is None or side == Side.SHORT):
            sp = self._paper_shorts.pop(symbol)
            price = await get_last_price(symbol) or sp.last_price
            collateral = abs(sp.qty) * sp.avg_price
            self._short_cash_lock = max(0.0, self._short_cash_lock - collateral)
            return OrderResult(
                True,
                f"paper-short-close-{symbol}",
                symbol,
                Side.SHORT,
                abs(sp.qty),
                price,
                "paper_short",
                "paper short closed",
            )

        # Close long on T212: sell with negative quantity
        try:
            positions = await self._get("/equity/positions")
            match = None
            if isinstance(positions, list):
                for p in positions:
                    ticker = p.get("ticker") or ""
                    if ticker.startswith(f"{symbol}_") or ticker.split("_")[0] == symbol:
                        match = p
                        break
            if not match:
                return OrderResult(False, "", symbol, Side.LONG, 0, 0, self.name, "position not found")
            qty = float(match.get("quantity") or 0)
            ticker = match["ticker"]
            price = float(match.get("currentPrice") or await get_last_price(symbol) or 0)
            resp = await self._post(
                "/equity/orders/market",
                {"ticker": ticker, "quantity": -abs(qty)},
            )
            if isinstance(resp, dict) and resp.get("ok") is False:
                return OrderResult(False, "", symbol, Side.LONG, qty, price, self.name, str(resp.get("body")), raw=resp)
            return OrderResult(
                True,
                str(resp.get("id") or "t212-close"),
                symbol,
                Side.LONG,
                -qty,
                price,
                self.name,
                "closed",
                raw=resp or {},
            )
        except Exception as e:
            return OrderResult(False, "", symbol, Side.LONG, 0, 0, self.name, str(e))

    async def flatten_all(self) -> list[OrderResult]:
        results: list[OrderResult] = []
        for sym in list(self._paper_shorts.keys()):
            results.append(await self.close_position(sym, Side.SHORT))
        try:
            positions = await self._get("/equity/positions")
            if isinstance(positions, list):
                for p in positions:
                    ticker = p.get("ticker") or ""
                    symbol = ticker.split("_")[0]
                    results.append(await self.close_position(symbol, Side.LONG))
        except Exception as e:
            results.append(OrderResult(False, "", "?", Side.LONG, 0, 0, self.name, str(e)))
        return results
