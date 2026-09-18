# CyberNexus

A platform for real-time detection that watches network activity, flags
suspicious behaviour, and shows it on a live map of the network.

## Stage 1 — real-time detection pipeline

[`stage1/`](stage1/) turns live connections into scored verdicts: capture, flow
assembly, feature extraction, inference, alerting, with benchmarks and tests.

Measured on a 10-core laptop: **96.99% detection at 1.31% false positives**,
**10,019 connections/s sustained** (ceiling 165k–185k/s), p50 end-to-end latency
8.6 ms.

## Stage 2 — live 3D network map

[`stage2/`](stage2/) discovers the device graph, correlates Stage 1 alerts to
devices, and renders the result in the browser.

Measured on the same machine: **101 ms** from alert to the host being marked on
screen, **60 fps** with 428 nodes, and a **46.7%** modelled reduction in
identification time versus a log console (45–55% across runs) — a model output,
with the method and its limits set out in [stage2/MTTI.md](stage2/MTTI.md).

```bash
cd stage1 && docker compose up --build     # detection pipeline
cd stage2 && docker compose up --build     # map at http://localhost:8080
```

Point both at the same Redis and Stage 2 consumes Stage 1's real alert stream.

| | |
|---|---|
| [stage1/README.md](stage1/README.md) | pipeline quickstart, architecture, configuration |
| [stage1/BENCHMARK.md](stage1/BENCHMARK.md) | measured detection and throughput results |
| [stage2/README.md](stage2/README.md) | map quickstart, discovery, API |
| [stage2/DESIGN.md](stage2/DESIGN.md) | UI rationale and scaling |
| [stage2/MTTI.md](stage2/MTTI.md) | the identification-time experiment |
