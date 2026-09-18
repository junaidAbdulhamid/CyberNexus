#!/usr/bin/env bash
# Start the Stage 2 backend for local dev and for the UI test suite.
# Reuses an already-running instance rather than failing on a bound port.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

PORT="${CN_MAP_PORT:-8080}"
if curl -fsS "http://localhost:${PORT}/api/health" >/dev/null 2>&1; then
  echo "backend already running on :${PORT}"
  exec sleep infinity
fi

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if [[ -x "../.venv/bin/python" ]]; then PYTHON="../.venv/bin/python"
  elif [[ -x ".venv/bin/python" ]]; then PYTHON=".venv/bin/python"
  else PYTHON="python3"; fi
fi

if [[ ! -f data/example-topology.json ]]; then
  echo "generating example topology..."
  PYTHONPATH=. "$PYTHON" -m discovery.synthesize --nodes 400
fi
if [[ ! -d frontend/dist ]]; then
  echo "building frontend..."
  (cd frontend && npm install --no-audit --no-fund && npm run build)
fi

export PYTHONPATH="$HERE"
exec "$PYTHON" -m uvicorn backend.app:app --host "${CN_MAP_HOST:-0.0.0.0}" --port "$PORT"
