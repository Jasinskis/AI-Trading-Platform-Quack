from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from urllib.parse import quote_plus

import feedparser
import httpx

from app.config import get_settings
from app.data.prices import load_top50

# Public RSS — keep a short, reliable set for speed
RSS_FEEDS = [
    ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("CNBC Top News", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("MarketWatch Top Stories", "https://feeds.marketwatch.com/marketwatch/topstories/"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
]


async def _fetch_rss(name: str, url: str) -> list[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "QuackTrader/1.0"})
            if r.status_code != 200:
                return []
            parsed = await asyncio.to_thread(feedparser.parse, r.text)
            items = []
            for e in parsed.entries[:12]:
                items.append(
                    {
                        "source": name,
                        "title": getattr(e, "title", ""),
                        "summary": getattr(e, "summary", "")[:400],
                        "link": getattr(e, "link", ""),
                        "published": getattr(e, "published", ""),
                    }
                )
            return items
    except Exception:
        return []


async def google_news_for_ticker(symbol: str) -> list[dict[str, Any]]:
    q = quote_plus(f"{symbol} stock")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    return await _fetch_rss(f"GoogleNews:{symbol}", url)


async def finnhub_news(symbol: str) -> list[dict[str, Any]]:
    key = get_settings().finnhub_api_key
    if not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://finnhub.io/api/v1/company-news",
                params={
                    "symbol": symbol,
                    "from": (datetime.utcnow().date().fromordinal(datetime.utcnow().date().toordinal() - 3)).isoformat(),
                    "to": datetime.utcnow().date().isoformat(),
                    "token": key,
                },
            )
            if r.status_code != 200:
                return []
            out = []
            for item in r.json()[:10]:
                out.append(
                    {
                        "source": "Finnhub",
                        "title": item.get("headline", ""),
                        "summary": (item.get("summary") or "")[:500],
                        "link": item.get("url", ""),
                        "published": str(item.get("datetime", "")),
                    }
                )
            return out
    except Exception:
        return []


async def gather_market_news() -> list[dict[str, Any]]:
    tasks = [_fetch_rss(name, url) for name, url in RSS_FEEDS]
    results = await asyncio.gather(*tasks)
    flat: list[dict[str, Any]] = []
    for batch in results:
        flat.extend(batch)
    return flat


async def gather_ticker_news(symbols: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    symbols = symbols or load_top50()
    # Limit concurrent Google News calls
    out: dict[str, list[dict[str, Any]]] = {}

    async def one(sym: str):
        g = await google_news_for_ticker(sym)
        f = await finnhub_news(sym)
        out[sym] = (g + f)[:15]

    # Batch in groups of 5 for speed / rate limits
    for i in range(0, len(symbols), 5):
        batch = symbols[i : i + 5]
        await asyncio.gather(*[one(s) for s in batch])
    return out


def filter_news_for_symbol(news: list[dict[str, Any]], symbol: str, aliases: list[str] | None = None) -> list[dict[str, Any]]:
    keys = {symbol.lower()}
    if aliases:
        keys.update(a.lower() for a in aliases)
    matched = []
    for n in news:
        text = f"{n.get('title','')} {n.get('summary','')}".lower()
        if any(k in text for k in keys):
            matched.append(n)
    return matched
