# CyberNexus — Stage 1: real-time network detection

A streaming detection pipeline that turns live connections into scored verdicts.
Packets become flow records, flow records become feature vectors, feature
vectors become alerts — each stage a separate process joined by Redis Streams,
each stage scalable on its own.

**Measured on a 10-core laptop** (full detail in [BENCHMARK.md](BENCHMARK.md)):

| | |
|---|---|
| Detection rate | **96.99%** at 1.31% false positives (ROC AUC 0.999) |
| Sustained throughput | **10,019 conn/s** with zero backlog and zero loss |
| Measured ceiling | **165k–185k conn/s** — generator-limited, not pipeline-limited |
| End-to-end latency | p50 8.6 ms, p99 22.7 ms |
| Resources for 10k/s | 1 ingest worker + 1 scorer worker + Redis |

Both acceptance criteria (≥96% detection, ≥10k conn/s) pass. What that number
does and does not establish is set out plainly in
[BENCHMARK.md §9](BENCHMARK.md#9-honesty-what-these-detection-numbers-do-and-do-not-establish) —
the short version is that detection is measured against a traffic simulator,
the simulator's difficulty is a documented knob, and the full degradation curve
is published rather than one flattering point on it.

---

## Quickstart

### Option 1 — Docker (nothing to install)

```bash
docker compose up --build
```

Brings up Redis, trains a model into a shared volume, then starts ingest,
scoring, the API and a synthetic traffic source. About three minutes on first
run, most of it training.

```bash
curl localhost:8000/health
curl 'localhost:8000/alerts?limit=5'
curl 'localhost:8000/alerts/summary?top=5'
```

Live alert feed over WebSocket:

```bash
python - <<'PY'
import asyncio, json, websockets
async def main():
    async with websockets.connect("ws://localhost:8000/ws/alerts") as ws:
        while True:
            for a in json.loads(await ws.recv())[:3]:
                print(f"{a['score']:.3f}  {a['src_ip']}:{a['src_port']} -> {a['dst_ip']}:{a['dst_port']}")
asyncio.run(main())
PY
```

Tear down with `docker compose down -v`.

### Option 2 — Local

```bash
make setup                  # venv + dependencies
make train                  # ~90 s, writes artifacts/model/
make test                   # 122 tests
make redis-up               # Redis in Docker (or run your own)
make bench                  # end-to-end benchmark at 10k conn/s
```

To run the pipeline by hand, one process per terminal:

```bash
python -m ingest.consumer --workers 2
python -m model.scorer --workers 2
uvicorn api.app:app --port 8000
python -m bench.generator --rate 10000 --duration 60     # or real capture, below
```

### Option 3 — No Redis at all

```bash
make bench-inproc           # whole pipeline on threads in one process
```

Useful in CI and for isolating pipeline compute cost from the bus.

---

## Capturing real traffic

```bash
# replay a capture file (no privileges needed)
python -m capture.pcap_capture --pcap /path/to/traffic.pcap

# live interface (needs root / CAP_NET_RAW)
sudo -E python -m capture.pcap_capture --iface en0

# from a pipe
sudo tcpdump -i en0 -w - | python -m capture.pcap_capture --stdin
```

The scapy-based capture path is a **correctness prototype**, measured at ~10.5k
packets/s (~1.8k connections/s) — roughly 10x short of a saturated gigabit link.
It is the only stage that needs replacing to go to line rate;
[capture/NOTES.md](capture/NOTES.md) covers Zeek, AF_PACKET fanout, eBPF/XDP and
DPDK, with the migration path for each. Everything downstream already runs at
>165k conn/s.

---

## Architecture

```
 packets ──► capture ──► cn:conns ──► ingest ──► cn:features ──► scorer ──┬─► cn:alerts ──► api ──► REST / WebSocket
 (pcap/live)  flow table            feature             inference         └─► cn:scores  (bench only)
              batching              extraction          + threshold
```

Records move as **msgpack batches**, never one at a time. At 10k conn/s with the
default 512-record batch that is ~20 bus messages per second per stage, and the
entire per-connection cost of the pipeline is ~3.1 µs on one core. Feature
extraction and inference together account for 0.58 µs of that — the design
decisions that matter are in serialisation and batching, not in the model.
[DESIGN.md](DESIGN.md) explains the reasoning, the alternatives, and the Kafka
port.

| directory | what it is |
|---|---|
| `cybernexus/` | shared library: record schema, bus, features, flow table, traffic model, stage runner |
| `capture/` | pcap/live capture exporter + production notes |
| `ingest/` | consumer group that aggregates flows and extracts features |
| `model/` | training pipeline, dataset adapters, inference wrapper, scoring service |
| `api/` | REST + WebSocket + Prometheus alerting service |
| `bench/` | traffic generator, end-to-end harness, difficulty sweep, profiler |
| `tests/` | 122 tests: unit, integration, and an end-to-end path with no external services |

---

## The model

A small MLP (64, 32) over **47 flow features** — duration, byte and packet
volumes and rates, directional asymmetry, TCP flag rates, inter-arrival
statistics, packet-size statistics and entropy, port characteristics, and TLS
and HTTP metadata. Trained by `model/train.py`, exported to ONNX (22 KiB), served
at 4.6M rows/s on one core.

`model/train.py` trains a RandomForest alongside it and picks between them on
measured quality *and* measured cost; the rule is in the file, and the decision
plus every candidate's metrics land in `artifacts/model/metadata.json`.

Two things are deliberately **not** features: IP addresses (a model that learns
which host is malicious has memorised the lab) and payload content (see
Privacy).

### Training on real data

The simulator is the default, but the same feature pipeline runs on the public
IDS datasets:

```bash
./model/get_dataset.sh unsw      # UNSW-NB15
./model/get_dataset.sh cic       # CIC-IDS-2017
python -m model.train --dataset unsw --data data/unsw
```

Swapping models is swapping a directory: `CN_MODEL_DIR=artifacts/model-unsw`.
Training and inference share no code, only the artifact.

---

## API

| endpoint | purpose |
|---|---|
| `GET /health` | liveness, bus reachability, loaded model |
| `GET /alerts` | recent alerts; filter by `min_score`, `src_ip`, `dst_port` |
| `GET /alerts/summary` | rollup by source address and destination port |
| `GET /stats` | alert counters, rates, buffer and backlog depth |
| `GET /metrics` | Prometheus exposition |
| `POST /score` | score a single connection record (micro-batched internally) |
| `WS /ws/alerts` | live alert feed |

---

## Configuration

Everything is environment-driven (`cybernexus/config.py`); the defaults are the
ones the benchmark was run with.

| variable | default | meaning |
|---|---|---|
| `CN_BUS` | `redis` | `redis` or `memory` |
| `CN_REDIS_URL` | `redis://localhost:6379/0` | bus endpoint |
| `CN_MODEL_DIR` | `artifacts/model` | model artifact directory |
| `CN_BACKEND` | `auto` | `onnx`, `joblib`, or prefer-ONNX `auto` |
| `CN_THRESHOLD` | from model | override the alerting threshold |
| `CN_PUBLISH_BATCH` | `512` | records per bus message — the latency/throughput dial |
| `CN_READ_BATCH` | `16` | messages a worker consumes at once |
| `CN_INGEST_WORKERS` / `CN_SCORER_WORKERS` | `2` | processes per stage |
| `CN_CPU_AFFINITY` | unset | e.g. `"2,3,4,5"` — pin workers to cores (Linux) |
| `CN_STREAM_MAXLEN` | `2000000` | approximate stream trim |
| `CN_FLOW_IDLE_TIMEOUT` / `CN_FLOW_ACTIVE_TIMEOUT` | `15` / `120` | flow expiry (seconds) |
| `CN_MAX_FLOWS` | `250000` | flow-table cap before forced expiry |
| `CN_STORE_PAYLOADS` | `0` | **opt-in** payload retention |
| `CN_ANONYMIZE_IPS` | `0` | pseudonymise addresses at capture |
| `CN_EMIT_SCORES` | `0` | publish every score (benchmark mode) |

---

## Security and privacy

* **Payloads are not retained.** No feature requires payload content:
  `payload_entropy` is the entropy of the flow's *packet-size distribution*.
* **Inspection is not retention.** Capture reads up to 512 bytes in memory to
  extract two pieces of metadata — whether a request looks like HTTP, and the
  *length* (never the value) of the TLS SNI — then discards them with the packet.
* **Opting in is explicit.** `CN_STORE_PAYLOADS=1` logs a warning at startup,
  because it changes what the system is.
* **Addresses can be pseudonymised** at capture (`CN_ANONYMIZE_IPS=1`), keyed
  and stable so flows still correlate per host. Since addresses are not
  features, this costs no detection quality.
* **Least privilege**: only capture needs `CAP_NET_RAW`; every other service and
  the container image run unprivileged.

## Known limitations

Stated up front rather than discovered later — the full list is
[DESIGN.md §8](DESIGN.md#8-known-limitations):

* detection is measured against a **simulator**, not real traffic;
* verdicts are **per flow**; at 10k conn/s a 1.3% false-positive rate is ~130
  alerts/s, which needs source-level correlation before an analyst sees it (top
  of the production list in [DESIGN.md §9](DESIGN.md#9-production-hardening--what-to-do-next-in-order));
* the capture prototype is ~10x too slow for a saturated gigabit link;
* the model is static — no drift detection, no retraining loop;
* the API is unauthenticated, by design for the demo.

## Documents

* [DESIGN.md](DESIGN.md) — rationale, trade-offs, scaling, Kafka variant, production roadmap
* [BENCHMARK.md](BENCHMARK.md) — every measured number, how to reproduce it, and what it proves
* [capture/NOTES.md](capture/NOTES.md) — production capture: Zeek, AF_PACKET, eBPF, DPDK
