#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.txt
export PYTHONPATH="$ROOT/backend"
if [[ ! -f "$ROOT/.env" ]]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "Created .env from example (MOCK_BROKER=true, MOCK_LLM=true recommended for first run)."
fi
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
