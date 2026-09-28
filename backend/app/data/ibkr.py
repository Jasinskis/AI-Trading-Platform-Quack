from __future__ import annotations

"""Optional IBKR delayed market data via ib_insync when TWS/Gateway is running."""

import asyncio
from typing import Optional

import pandas as pd

from app.config import get_settings

_ib = None
_lock = asyncio.Lock()


async def _get_ib():
    global _ib
    settings = get_settings()
    if not settings.ibkr_enabled:
        return None
    async with _lock:
        if _ib is not None and _ib.isConnected():
            return _ib
        try:
            from ib_insync import IB

            ib = IB()
            await ib.connectAsync(
                settings.ibkr_host,
                settings.ibkr_port,
                clientId=settings.ibkr_client_id,
            )
            _ib = ib
            return _ib
        except Exception:
            _ib = None
            return None


async def ibkr_last_price(symbol: str) -> Optional[float]:
    ib = await _get_ib()
    if not ib:
        return None
    from ib_insync import Stock

    contract = Stock(symbol.replace(".", " "), "SMART", "USD")
    await ib.qualifyContractsAsync(contract)
    tickers = await ib.reqTickersAsync(contract)
    if not tickers:
        return None
    t = tickers[0]
    px = t.marketPrice()
    if px and px == px:  # not NaN
        return float(px)
    if t.close:
        return float(t.close)
    return None


async def ibkr_bars(symbol: str, interval: str = "5m", period: str = "5d") -> Optional[pd.DataFrame]:
    ib = await _get_ib()
    if not ib:
        return None
    from ib_insync import Stock, util

    bar_map = {
        "1m": "1 min",
        "5m": "5 mins",
        "15m": "15 mins",
        "30m": "30 mins",
        "1h": "1 hour",
        "1d": "1 day",
    }
    duration_map = {
        "1d": "1 D",
        "5d": "5 D",
        "1mo": "1 M",
        "3mo": "3 M",
    }
    contract = Stock(symbol.replace(".", " "), "SMART", "USD")
    await ib.qualifyContractsAsync(contract)
    bars = await ib.reqHistoricalDataAsync(
        contract,
        endDateTime="",
        durationStr=duration_map.get(period, "5 D"),
        barSizeSetting=bar_map.get(interval, "5 mins"),
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1,
    )
    if not bars:
        return None
    df = util.df(bars)
    if df is None or df.empty:
        return None
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
    return df
