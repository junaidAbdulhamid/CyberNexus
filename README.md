# CyberNexus

A platform for real-time detection that watches network activity and flags
suspicious behaviour.

## Stage 1 — real-time detection pipeline

[`stage1/`](stage1/) holds the working system: a streaming pipeline that turns
live connections into scored verdicts, with capture, feature extraction,
inference, alerting, benchmarks and tests.

Measured on a 10-core laptop: **96.99% detection at 1.31% false positives**,
**10,019 connections/s sustained** with a measured ceiling of 165k–185k/s, and
p50 end-to-end latency of 8.6 ms.

```bash
cd stage1
docker compose up --build        # full demo: redis + pipeline + API
# or
make setup && make train && make test && make bench
```

* [stage1/README.md](stage1/README.md) — quickstart, architecture, configuration
* [stage1/DESIGN.md](stage1/DESIGN.md) — design rationale and production roadmap
* [stage1/BENCHMARK.md](stage1/BENCHMARK.md) — measured results and how to reproduce them
