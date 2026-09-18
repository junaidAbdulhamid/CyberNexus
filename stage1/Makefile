# CyberNexus Stage 1
#
#   make setup      create .venv and install dependencies
#   make train      train the model into artifacts/model
#   make test       run the test suite
#   make bench      end-to-end benchmark at 10k conn/s (needs Redis)
#   make demo       docker compose stack: redis + capture + ingest + scorer + api

PY ?= python3
VENV ?= ../.venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
RATE ?= 10000
DURATION ?= 30
REDIS_URL ?= redis://localhost:6379/0

export PYTHONPATH := $(CURDIR)

.PHONY: help setup train test test-fast bench bench-max bench-inproc bench-drift sweep \
        profile api capture demo demo-down redis-up redis-down clean report

help:
	@grep -E '^#   ' Makefile | sed 's/^#   //'

setup:
	$(PY) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@echo "done. activate with: source $(VENV)/bin/activate"

train:
	$(PYTHON) -m model.train --out artifacts/model

train-quick:
	$(PYTHON) -m model.train --n 100000 --model mlp --out artifacts/model

test:
	$(PYTHON) -m pytest

test-fast:
	$(PYTHON) -m pytest -m "not slow"

# --- benchmarks ---------------------------------------------------------
bench: redis-check
	$(PYTHON) -m bench.run_bench --rate $(RATE) --duration $(DURATION)

bench-max: redis-check
	$(PYTHON) -m bench.run_bench --rate 0 --duration 20 \
		--gen-workers 3 --ingest-workers 3 --scorer-workers 2

bench-drift: redis-check
	$(PYTHON) -m bench.run_bench --rate $(RATE) --duration $(DURATION) --drift 0.4 \
		--out artifacts/bench-drift

bench-inproc:
	$(PYTHON) -m bench.inproc_bench --total 200000

sweep:
	$(PYTHON) -m bench.difficulty_sweep --n 200000 --out artifacts/difficulty_sweep.json

profile:
	$(PYTHON) -m bench.profile_pipeline --stages --batch 512 --repeats 200

profile-detail:
	$(PYTHON) -m bench.profile_pipeline --profile --batch 512 --repeats 200

infer-bench:
	$(PYTHON) -m model.infer --backend onnx
	$(PYTHON) -m model.infer --backend joblib

# --- services -----------------------------------------------------------
api:
	$(VENV)/bin/uvicorn api.app:app --host 0.0.0.0 --port 8000

ingest:
	$(PYTHON) -m ingest.consumer --workers 2

scorer:
	$(PYTHON) -m model.scorer --workers 2

capture:
	sudo $(PYTHON) -m capture.pcap_capture --iface $(IFACE)

replay:
	$(PYTHON) -m capture.pcap_capture --pcap $(PCAP)

generate:
	$(PYTHON) -m bench.generator --rate $(RATE) --duration $(DURATION)

# --- infrastructure -----------------------------------------------------
redis-up:
	docker run -d --name cn-redis -p 6379:6379 redis:7-alpine --save '' --appendonly no

redis-down:
	-docker rm -f cn-redis

redis-check:
	@$(PYTHON) -c "import redis,sys,os; \
	  sys.exit(0) if redis.Redis.from_url(os.getenv('CN_REDIS_URL','$(REDIS_URL)')).ping() else sys.exit(1)" \
	  2>/dev/null || { echo "Redis is not reachable at $(REDIS_URL). Try: make redis-up"; exit 1; }

demo:
	docker compose up --build

demo-down:
	docker compose down -v

clean:
	rm -rf artifacts/bench artifacts/bench-drift .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
