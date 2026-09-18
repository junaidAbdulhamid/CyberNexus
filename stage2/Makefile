# CyberNexus Stage 2 — live 3D network map
#
#   make setup        install python + node dependencies
#   make demo         docker compose stack (redis + discovery + backend + alerts)
#   make frontend-demo  build the backend-free static bundle (Vercel etc.)
#   make dev          run backend and frontend locally with hot reload
#   make test         python tests + UI tests
#   make mtti         run the MTTI experiment and print the report

PY ?= python3
VENV ?= ../.venv
PYTHON := $(VENV)/bin/python
PORT ?= 8080
TRIALS ?= 30
NOISE ?= 60

export PYTHONPATH := $(CURDIR)

.PHONY: help setup topology backend frontend dev demo demo-down test test-py test-ui \
        test-visual snapshots mtti discovery lint clean redis-up redis-down

help:
	@grep -E '^#   ' Makefile | sed 's/^#   //'

setup:
	$(PY) -m venv $(VENV) 2>/dev/null || true
	$(VENV)/bin/pip install -q -r requirements.txt
	cd frontend && npm install --no-audit --no-fund
	cd ui-tests && npm install --no-audit --no-fund

topology:
	$(PYTHON) -m discovery.synthesize --nodes 400 --out data/example-topology.json

frontend:
	cd frontend && npm run build

# Backend-free static bundle for a serverless host (see README, "Deploying").
frontend-demo:
	cd frontend && npm run build:demo

preview-demo:
	cd frontend && npm run preview:demo

backend:
	bash scripts/start-backend.sh

dev:
	@echo "run in two terminals:"
	@echo "  1) make backend"
	@echo "  2) cd frontend && npm run dev     # http://localhost:5173"

# --- tests --------------------------------------------------------------
test: test-py test-ui

test-py:
	$(PYTHON) -m pytest

test-ui:
	cd ui-tests && npx playwright test

test-visual:
	cd ui-tests && npx playwright test tests/visual.spec.js

# Needs `make preview-demo` running in another terminal.
test-demo:
	cd ui-tests && npx playwright test tests/demo.spec.js

snapshots:
	cd ui-tests && npx playwright test tests/visual.spec.js --update-snapshots

# --- experiment ---------------------------------------------------------
mtti:
	cd ui-tests && node mtti/run-mtti.js --trials $(TRIALS) --noise $(NOISE) --out results/mtti.json

# --- live discovery -----------------------------------------------------
discovery:
	$(PYTHON) -m discovery.run_discovery --out data/discovered-topology.json

# --- infrastructure -----------------------------------------------------
redis-up:
	docker run -d --name cn-redis -p 6379:6379 redis:7-alpine --save '' --appendonly no

redis-down:
	-docker rm -f cn-redis

demo:
	docker compose up --build

demo-down:
	docker compose down -v

clean:
	rm -rf ui-tests/results frontend/dist .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
