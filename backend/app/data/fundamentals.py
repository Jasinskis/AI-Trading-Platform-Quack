from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

from app.config import get_settings
from app.data.prices import load_top50


async def fundamentals_for(symbol: str) -> dict[str, Any]:
    settings = get_settings()
    if settings.fmp_api_key:
        data = await _fmp(symbol, settings.fmp_api_key)
        if data:
            return data
    return await asyncio.to_thread(_yf_fundamentals, symbol)


def _yf_fundamentals(symbol: str) -> dict[str, Any]:
    try:
        import yfinance as yf

        t = yf.Ticker(symbol.replace(".", "-"))
        info = t.info or {}
        cal = {}
        try:
            cal = t.calendar if isinstance(t.calendar, dict) else {}
        except Exception:
            cal = {}
        return {
            "symbol": symbol,
            "name": info.get("longName") or info.get("shortName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "market_cap": info.get("marketCap"),
            "pe": info.get("trailingPE") or info.get("forwardPE"),
            "forward_pe": info.get("forwardPE"),
            "peg": info.get("pegRatio"),
            "ps": info.get("priceToSalesTrailing12Months"),
            "pb": info.get("priceToBook"),
            "roe": info.get("returnOnEquity"),
            "profit_margin": info.get("profitMargins"),
            "revenue_growth": info.get("revenueGrowth"),
            "earnings_growth": info.get("earningsGrowth"),
            "dividend_yield": info.get("dividendYield"),
            "ex_dividend_date": str(info.get("exDividendDate") or ""),
            "earnings_date": str(cal.get("Earnings Date") or info.get("earningsTimestamp") or ""),
            "debt_to_equity": info.get("debtToEquity"),
            "free_cashflow": info.get("freeCashflow"),
            "recommendation": info.get("recommendationKey"),
        }
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}


async def _fmp(symbol: str, key: str) -> Optional[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            profile = await client.get(
                f"https://financialmodelingprep.com/api/v3/profile/{symbol}",
                params={"apikey": key},
            )
            ratios = await client.get(
                f"https://financialmodelingprep.com/api/v3/ratios-ttm/{symbol}",
                params={"apikey": key},
            )
            p = profile.json()[0] if profile.status_code == 200 and profile.json() else {}
            r = ratios.json()[0] if ratios.status_code == 200 and ratios.json() else {}
            return {
                "symbol": symbol,
                "name": p.get("companyName"),
                "sector": p.get("sector"),
                "industry": p.get("industry"),
                "market_cap": p.get("mktCap"),
                "pe": r.get("peRatioTTM") or p.get("pe"),
                "pb": r.get("priceToBookRatioTTM"),
                "roe": r.get("roeTTM"),
                "dividend_yield": p.get("lastDiv"),
                "source": "fmp",
            }
    except Exception:
        return None


async def batch_fundamentals(symbols: list[str] | None = None) -> dict[str, dict[str, Any]]:
    symbols = symbols or load_top50()
    out: dict[str, dict[str, Any]] = {}

    async def one(s: str):
        try:
            out[s] = await fundamentals_for(s)
        except Exception as e:
            out[s] = {"symbol": s, "error": str(e)}

    # Small batches + pause to avoid Yahoo 429 rate limits
    for i in range(0, len(symbols), 2):
        await asyncio.gather(*[one(s) for s in symbols[i : i + 2]])
        if i + 2 < len(symbols):
            await asyncio.sleep(0.6)
    return out


def sector_averages(fund_map: dict[str, dict[str, Any]]) -> dict[str, dict[str, float]]:
    buckets: dict[str, list[dict]] = {}
    for f in fund_map.values():
        sector = f.get("sector") or "Unknown"
        buckets.setdefault(sector, []).append(f)
    avgs: dict[str, dict[str, float]] = {}
    for sector, items in buckets.items():
        def mean(key: str) -> float:
            vals = [float(x[key]) for x in items if isinstance(x.get(key), (int, float))]
            return sum(vals) / len(vals) if vals else 0.0

        avgs[sector] = {
            "pe": mean("pe"),
            "pb": mean("pb"),
            "roe": mean("roe"),
            "profit_margin": mean("profit_margin"),
        }
    return avgs
