# Quack desk — how data & trades actually work

Agents do NOT call Yahoo, T212, or browsers themselves. The Quack engine pulls data and executes.
Your job is to reason on the packages the engine gives you and produce correct JSON.

## Data sources (engine)
- **Universe**: top-10 SPX only (`config/top10.json`).
- **News (Sentiment package)**: public RSS (BBC, CNBC, MarketWatch, Yahoo, Google News, Finnhub if keyed).
- **Technicals (Technicals package)**: Yahoo Finance chart API OHLCV (typically ~15m delayed). Empty/`error` means rate-limit or gap — still give a view and note the gap; do not refuse the whole day.
- **Fundamentals (Fundamentals package)**: Yahoo/`info` (+ FMP if keyed). Same rule on gaps.
- **Live marks**: Yahoo chart last price (primary); Finnhub/FMP if API keys set; optional IBKR. **Never invent $100 fills** — no quote → skip the order.
- **After-hours / simulate**: MockBroker + Yahoo marks. Fills are paper — NOT live T212.

## Execution path (engine)
1. Strategist proposes `trades[]` (symbol, side, allocation_pct ≤ 0.30, entry_timing, reason).
2. Committee votes (Strategist, Risk, Desk) — majority.
3. **Executor** (code) sends `market_order` / close via T212 demo or MockBroker.
4. Book marks book; Risk may escalate intraday; flatten at close → Desk library.

## Hard rules for all agents
- If one research stream fails/empty, **use the others**. Never return zero trades solely because a peer failed when any usable bias/score/technicals exist.
- Operator asked to simulate / trade / full process → propose **at least 2 and at most 5** high-conviction names unless risk forbids (then explain in narrative).
- Prefer Sentiment+Technicals agreement; Fundamentals is earnings/event filter (priority 3).
- Output **valid compact JSON only** (no markdown fences, no trailing commentary).
- Shorts are allowed in sim / paper-short ledger; T212 Invest is long-only for live cash legs.
