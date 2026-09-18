#!/usr/bin/env bash
# End-to-end benchmark: train if needed, check Redis, run, report.
#
#   ./bench/run_bench.sh                 # 10k conn/s for 30s
#   ./bench/run_bench.sh 25000 60        # 25k conn/s for 60s
#   ./bench/run_bench.sh 0 20            # unthrottled: find the ceiling
set -euo pipefail

RATE="${1:-10000}"
DURATION="${2:-30}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
export PYTHONPATH="$HERE"

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if [[ -x "../.venv/bin/python" ]]; then PYTHON="../.venv/bin/python"
  elif [[ -x ".venv/bin/python" ]]; then PYTHON=".venv/bin/python"
  else PYTHON="python3"; fi
fi

REDIS_URL="${CN_REDIS_URL:-redis://localhost:6379/0}"
if ! "$PYTHON" - <<PY 2>/dev/null
import os, sys, redis
sys.exit(0 if redis.Redis.from_url("$REDIS_URL").ping() else 1)
PY
then
  echo "Redis is not reachable at $REDIS_URL."
  echo "Start one with:  docker run -d --name cn-redis -p 6379:6379 redis:7-alpine"
  echo "Or run the no-Redis pipeline benchmark:  $PYTHON -m bench.inproc_bench"
  exit 1
fi

MODEL_DIR="${CN_MODEL_DIR:-artifacts/model}"
if [[ ! -f "$MODEL_DIR/metadata.json" ]]; then
  echo "No model in $MODEL_DIR - training one first (a couple of minutes)..."
  "$PYTHON" -m model.train --out "$MODEL_DIR"
fi

echo "Benchmarking: rate=${RATE} conn/s duration=${DURATION}s"
exec "$PYTHON" -m bench.run_bench --rate "$RATE" --duration "$DURATION"
