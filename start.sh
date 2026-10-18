#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PY=python3
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
fi

exec "$PY" -m uvicorn server.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-3000}"
