#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Backend
(
  cd "$ROOT/backend"
  [[ -d .venv ]] || python3 -m venv .venv
  source .venv/bin/activate
  pip install -q -r requirements.txt
  export PYTHONPATH="$ROOT/backend"
  [[ -f "$ROOT/.env" ]] || cp "$ROOT/.env.example" "$ROOT/.env"
  uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
) &
BACK_PID=$!

# Frontend
(
  cd "$ROOT/frontend"
  [[ -d node_modules ]] || npm install
  npm run dev -- --host 127.0.0.1 --port 5173
) &
FRONT_PID=$!

trap 'kill $BACK_PID $FRONT_PID 2>/dev/null' EXIT
echo "Quack desk: UI http://127.0.0.1:5173  API http://127.0.0.1:8000"
wait
