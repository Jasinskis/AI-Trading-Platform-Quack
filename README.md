# Quack — Multi-Agent Equity Desk

Autonomous US equity day-trading desk: Claude agents, Trading 212 Demo (optional), Yahoo marks, React dashboard, Telegram alerts.

## Agents (roles)

| Role | Job |
|------|-----|
| **Sentiment** | News / sentiment |
| **Technicals** | Independent TA |
| **Fundamentals** | Fundamentals & events |
| **Strategist** | Day plan |
| **Executor** | Order execution |
| **Book** | Live book / marks |
| **Risk** | Risk & escalations |
| **Desk** | Brief parse, votes, EOD library |
| **Floor** | Operator commentator (no trading) |

Committee = Strategist + Risk + Desk (majority) before Executor acts.

## Quick start

```bash
cp .env.example .env   # fill keys locally — never commit .env

cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH=.
uvicorn app.main:app --reload --port 8000

# other terminal
cd frontend
npm install
npm run dev
```

For a dry run without Anthropic/T212: set `MOCK_LLM=true` and `MOCK_BROKER=true` in `.env`.

## Operator brief

Freeform text in the UI. Desk parses capital, timing, and style. Caps: ≤30% per name, ≤10 names, top-10 SPX, flatten at close unless overridden.

## Secrets

Copy `.env.example` → `.env` and add:

- `ANTHROPIC_API_KEY`
- Optional: Trading 212 Demo keys, Telegram, Finnhub, FMP, IBKR

`.env`, databases, and runtime state are gitignored. Do not commit API keys.
