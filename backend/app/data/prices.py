from __future__ import annotations

import asyncio
import json
import ssl
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd

from app.config import get_settings

_price_cache: dict[str, tuple[float, datetime]] = {}
_bar_cache: dict[str, tuple[pd.DataFrame, datetime]] = {}
_ssl_ctx: ssl.SSLContext | None = None


def _ssl() -> ssl.SSLContext:
    global _ssl_ctx
    if _ssl_ctx is None:
        try:
            import certifi

            _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


def _ysym(symbol: str) -> str:
    return symbol.replace(".", "-")


def _http_get(url: str, timeout: float = 12.0) -> bytes:
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; QuackDesk/1.0)",
            "Accept": "application/json,text/plain,*/*",
        },
        method="GET",
    )
    with urlopen(req, timeout=timeout, context=_ssl()) as resp:
        return resp.read()


def _yahoo_chart_last(symbol: str) -> Optional[float]:
    """Direct Yahoo chart endpoint — more reliable than yfinance on broken SSL setups."""
    ysym = _ysym(symbol)
    for range_ in ("1d", "5d"):
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?interval=1m&range={range_}"
        try:
            raw = _http_get(url, timeout=10)
            data = json.loads(raw.decode("utf-8"))
            result = (data.get("chart") or {}).get("result") or []
            if not result:
                continue
            meta = result[0].get("meta") or {}
            for key in ("regularMarketPrice", "postMarketPrice", "preMarketPrice", "previousClose", "chartPreviousClose"):
                px = meta.get(key)
                if px is not None:
                    return float(px)
            # last close from quote series
            closes = (((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close")) or []
            for c in reversed(closes):
                if c is not None:
                    return float(c)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    return None


def _yahoo_chart_bars(symbol: str, interval: str, period: str) -> pd.DataFrame:
    ysym = _ysym(symbol)
    # Map common periods to Yahoo ranges
    range_map = {
        "1d": "1d",
        "5d": "5d",
        "1mo": "1mo",
        "3mo": "3mo",
        "6mo": "6mo",
        "1y": "1y",
        "2y": "2y",
        "5y": "5y",
    }
    rng = range_map.get(period, period if period in range_map.values() else "5d")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?interval={interval}&range={rng}"
    try:
        data = json.loads(_http_get(url, timeout=12).decode("utf-8"))
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return pd.DataFrame()
        ts = result[0].get("timestamp") or []
        quote = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
        if not ts:
            return pd.DataFrame()
        df = pd.DataFrame(
            {
                "Open": quote.get("open"),
                "High": quote.get("high"),
                "Low": quote.get("low"),
                "Close": quote.get("close"),
                "Volume": quote.get("volume"),
            },
            index=pd.to_datetime(ts, unit="s", utc=True),
        )
        return df.dropna(how="all")
    except Exception:
        return pd.DataFrame()


def _finnhub_last(symbol: str) -> Optional[float]:
    settings = get_settings()
    key = (settings.finnhub_api_key or "").strip()
    if not key:
        return None
    url = f"https://finnhub.io/api/v1/quote?symbol={_ysym(symbol)}&token={key}"
    try:
        data = json.loads(_http_get(url, timeout=8).decode("utf-8"))
        px = data.get("c") or data.get("pc")
        return float(px) if px else None
    except Exception:
        return None


def _fmp_last(symbol: str) -> Optional[float]:
    settings = get_settings()
    key = (settings.fmp_api_key or "").strip()
    if not key:
        return None
    ysym = _ysym(symbol)
    for url in (
        f"https://financialmodelingprep.com/api/v3/quote-short/{ysym}?apikey={key}",
        f"https://financialmodelingprep.com/api/v3/quote/{ysym}?apikey={key}",
    ):
        try:
            data = json.loads(_http_get(url, timeout=8).decode("utf-8"))
            if isinstance(data, list) and data:
                row = data[0]
                px = row.get("price") or row.get("c")
                if px:
                    return float(px)
        except Exception:
            continue
    return None


def _yf_last_price(symbol: str) -> Optional[float]:
    try:
        import yfinance as yf

        t = yf.Ticker(_ysym(symbol))
        info = t.fast_info
        px = getattr(info, "last_price", None) or getattr(info, "lastPrice", None)
        if px:
            return float(px)
        hist = t.history(period="5d")
        if hist is not None and not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        return None
    return None


def _sync_last_price(symbol: str) -> Optional[float]:
    for fn in (_yahoo_chart_last, _finnhub_last, _fmp_last, _yf_last_price):
        try:
            px = fn(symbol)
            if px and px > 0:
                return float(px)
        except Exception:
            continue
    return None


async def get_last_price(symbol: str) -> Optional[float]:
    cached = _price_cache.get(symbol)
    if cached and datetime.utcnow() - cached[1] < timedelta(seconds=15):
        return cached[0]

    settings = get_settings()
    if settings.ibkr_enabled:
        try:
            from app.data.ibkr import ibkr_last_price

            px = await asyncio.wait_for(ibkr_last_price(symbol), timeout=5)
            if px:
                _price_cache[symbol] = (px, datetime.utcnow())
                return px
        except Exception:
            pass

    try:
        px = await asyncio.wait_for(asyncio.to_thread(_sync_last_price, symbol), timeout=12)
    except Exception:
        px = None
    if px and px > 0:
        _price_cache[symbol] = (float(px), datetime.utcnow())
        return float(px)
    return None


async def get_last_prices(symbols: list[str]) -> dict[str, float]:
    """Batch last prices; skips missing rather than inventing placeholders."""
    out: dict[str, float] = {}

    async def one(s: str) -> None:
        px = await get_last_price(s)
        if px:
            out[s] = px

    # modest concurrency to avoid hammering Yahoo
    for i in range(0, len(symbols), 4):
        await asyncio.gather(*[one(s) for s in symbols[i : i + 4]])
        if i + 4 < len(symbols):
            await asyncio.sleep(0.15)
    return out


async def get_bars(
    symbol: str,
    interval: str = "5m",
    period: str = "5d",
    *,
    max_cache_seconds: int = 300,
) -> pd.DataFrame:
    key = f"{symbol}:{interval}:{period}"
    cached = _bar_cache.get(key)
    if cached and datetime.utcnow() - cached[1] < timedelta(seconds=max_cache_seconds):
        return cached[0]

    settings = get_settings()
    if settings.ibkr_enabled:
        try:
            from app.data.ibkr import ibkr_bars

            df = await ibkr_bars(symbol, interval=interval, period=period)
            if df is not None and not df.empty:
                _bar_cache[key] = (df, datetime.utcnow())
                return df
        except Exception:
            pass

    df = await asyncio.to_thread(_yahoo_chart_bars, symbol, interval, period)
    if df is None or df.empty:
        df = await asyncio.to_thread(_yf_bars, symbol, interval, period)
    if df is None:
        df = pd.DataFrame()
    _bar_cache[key] = (df, datetime.utcnow())
    return df


def _yf_bars(symbol: str, interval: str, period: str) -> pd.DataFrame:
    try:
        import yfinance as yf

        df = yf.download(
            _ysym(symbol),
            interval=interval,
            period=period,
            progress=False,
            auto_adjust=True,
            threads=False,
        )
        if df is None or getattr(df, "empty", True):
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception:
        return pd.DataFrame()


def technical_snapshot(df: pd.DataFrame) -> dict[str, Any]:
    try:
        if df is None or df.empty or "Close" not in df.columns:
            return {"error": "no data", "source": "yfinance"}
        close = df["Close"].dropna()
        if len(close) < 5:
            return {"error": "insufficient bars", "source": "yfinance"}
        rets = close.pct_change().dropna()
        vol = float(rets.std() * (len(rets) ** 0.5)) if len(rets) else 0
        last = float(close.iloc[-1])
        first = float(close.iloc[0])
        sma_fast = float(close.tail(min(20, len(close))).mean())
        sma_slow = float(close.tail(min(50, len(close))).mean()) if len(close) >= 20 else sma_fast
        high = float(close.max())
        low = float(close.min())
        tail = [float(x) for x in close.tail(6).tolist()]
        return {
            "last": last,
            "period_return_pct": ((last / first) - 1) * 100,
            "volatility": vol,
            "sma_fast": sma_fast,
            "sma_slow": sma_slow,
            "trend": "up" if sma_fast >= sma_slow else "down",
            "range_high": high,
            "range_low": low,
            "bars": len(close),
            "last_closes": tail,
            "source": "yahoo_chart",
            "note": "Yahoo US equity quotes/candles are typically ~15 minutes delayed (not true tick live).",
        }
    except Exception as e:
        return {"error": str(e), "source": "yahoo_chart"}


async def live_candle_pack(symbols: list[str]) -> dict[str, Any]:
    """Fresh-ish 5m + 15m snapshots for live desk decisions (Yahoo delayed)."""
    out: dict[str, Any] = {}
    for symbol in symbols:
        try:
            bars_5 = await get_bars(symbol, interval="5m", period="1d", max_cache_seconds=60)
            bars_15 = await get_bars(symbol, interval="15m", period="5d", max_cache_seconds=90)
            bars_1h = await get_bars(symbol, interval="60m", period="5d", max_cache_seconds=120)
            out[symbol] = {
                "5m": technical_snapshot(bars_5),
                "15m": technical_snapshot(bars_15),
                "1h": technical_snapshot(bars_1h),
                "delayed": True,
                "provider": "yahoo_chart",
            }
        except Exception as e:
            out[symbol] = {"error": str(e), "delayed": True, "provider": "yahoo_chart"}
    return out


@lru_cache
def load_top50() -> list[str]:
    """Universe loader (name kept for compatibility). Uses top 10 SPX list."""
    import json as _json
    from app.config import CONFIG_DIR

    path = CONFIG_DIR / "top10.json"
    if not path.exists():
        path = CONFIG_DIR / "top50.json"
    data = _json.loads(path.read_text())
    return list(data["tickers"])


def load_universe() -> list[str]:
    return load_top50()
