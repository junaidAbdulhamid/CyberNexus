# Design rationale

This document explains why the system is shaped the way it is, what the
alternatives were, and what would have to change to run it on a real network.
Measured numbers live in [BENCHMARK.md](BENCHMARK.md).

## 1. The shape of the problem

The requirement — flag suspicious connections in real time, ≥96% detection,
10k+ connections/s — pulls in two directions.  Detection quality wants rich
per-flow features and a model with capacity.  Throughput wants as little
per-record Python as physically possible.

The resolution used throughout: **nothing in the hot path touches one record at
a time.**  Records travel as batches, batches convert to matrices in one call,
and every transformation is a numpy expression over columns.  The per-record
cost of the entire pipeline (deserialise → features → inference → serialise)
is about 3.1 µs, measured by `bench/profile_pipeline.py`.  That is what makes
five-figure rates achievable in CPython without rewriting anything in C.

## 2. Pipeline

```
 packets ──► capture ──► cn:conns ──► ingest ──► cn:features ──► scorer ──┬─► cn:alerts ──► api ──► REST / WebSocket
 (pcap/live)  flow table            feature             inference         └─► cn:scores  (bench only)
              batching              extraction          + threshold
```

Each arrow is a Redis Stream carrying **msgpack batches**, not individual
records.  At 10k conn/s with the default 512-record batch that is ~20 messages
per second per stage, so bus overhead is negligible (0.01 µs/record measured)
and Redis is nowhere near being the bottleneck.

Stages are separate processes joined only by the bus, which buys three things:
independent scaling (feature extraction and inference have very different
costs), crash isolation, and the ability to restart the model without dropping
capture.  The cost is one extra serialisation hop, measured at ~0.44 µs/record.
For a single-box deployment `ingest --fused` removes that hop and scores
in-process.

## 3. Technology choices

### Python for the pipeline and the model

Chosen because the numeric work is vectorised and therefore already in C, and
because the parts that matter for correctness (feature semantics, flow
assembly) are the parts you most want to be readable and testable.  Where Python
would have been the bottleneck — per-packet capture — the design says so
explicitly and hands off to Zeek/eBPF/DPDK (`capture/NOTES.md`).

### Redis Streams as the default bus

Redis Streams give consumer groups, at-least-once delivery with explicit acks, a
pending-entries list for recovering work from a dead consumer (`XAUTOCLAIM`, wired
up in `StageRunner._recover_pending`), and `MAXLEN ~` trimming to bound memory.
That is the complete feature set this pipeline needs, from a dependency most
teams already run.

**Kafka variant.** Switch when you need any of: retention measured in days
rather than memory, multiple independent consumer applications replaying the
same stream, partition counts beyond a single Redis instance's throughput, or
cross-datacentre replication.  The port is contained: implement `Bus` in
`cybernexus/bus.py` over `confluent-kafka`, map stream → topic, consumer group →
consumer group, and message id → `(partition, offset)`.  Key each message by
`flow_id` so a connection's records always land on one partition, and set
`enable.auto.commit=false` so the existing ack points stay meaningful.  Nothing
above the `Bus` interface changes.

**In-memory variant.** `MemoryBus` implements the same interface with the same
semantics (per-group cursors, pending sets, blocking reads, trimming).  It is
what makes the whole pipeline testable with no services running, and it is the
reason `tests/test_e2e.py` can exercise the real stage classes.

### scikit-learn MLP, exported to ONNX

`model/train.py` trains two candidates and picks between them on measured
quality and measured cost:

| model | detection @ calibrated threshold | F1 | inference | notes |
|---|---|---|---|---|
| MLP (64, 32) + StandardScaler | 0.9700 | 0.9551 | 4.59M rows/s (ONNX) | selected |
| RandomForest, 200 trees | 0.9701 | 0.9582 | 38k rows/s (sklearn) | +0.0031 F1, 84x the cost |

The selection rule is written down in the trainer: take the cheaper model unless
the forest beats it by more than `--select-margin` F1.  Here the forest's
advantage is inside the margin while costing ~84x more per row, so the MLP
wins.  (The rule is data-dependent: the Docker demo trains on 300k flows and
selects the forest.  `artifacts/model/metadata.json` always records which model
was chosen and why.)  On a different dataset the rule may well pick the forest — that is the
point of having a rule rather than a preference.

A 1D-CNN or transformer over packet sequences was not pursued.  The brief says
to reach for one only if needed for accuracy, and at ROC AUC 0.999 on this
feature set the headroom is in the irreducible class overlap, not in model
capacity.  Sequence models would also require retaining per-packet history,
which conflicts with the no-payload-retention default.

ONNX Runtime is the serving backend because the exported graph carries the
scaler *inside* it — the serving path cannot drift from the training path — and
because a single-threaded session is ~1.5x faster than the sklearn pipeline
(4.59M vs 3.12M rows/s) while using one core instead of many.  `joblib` remains as a fallback, and
`tests/test_infer.py::test_onnx_and_joblib_agree` asserts the two agree to 1e-4.

## 4. Features

47 features, all derived from flow metadata, listed in
`cybernexus/features.py::FEATURE_NAMES`:

* **volume and rate** — duration, byte/packet counts per direction (log-scaled,
  since they are heavy-tailed), bytes per packet, bytes and packets per second;
* **asymmetry** — response/request byte and packet ratios, which is what
  separates a scan (nothing comes back) from exfiltration (nothing goes back);
* **TCP flag rates** — SYN/ACK/FIN/RST/PSH/URG as a fraction of packets, so a
  half-open scan looks different from a completed session regardless of size;
* **timing** — inter-arrival mean/std/min/max and the coefficient of variation.
  `iat_cv` is the single most useful beaconing feature: a human session is
  bursty (cv > 1), an implant checking in on a timer is not (cv ≈ 0);
* **packet size** — mean/std/min/max/range and the Shannon entropy of the
  packet-size distribution.  Scripted traffic replays near-identical sizes;
* **ports** — well-known/registered/ephemeral bands plus indicators for the
  handful of ports that carry real signal (22, 53, 80, 443, 445, 3389);
* **protocol metadata** — TLS present + SNI *length*, HTTP present + request
  count.

Two deliberate omissions.  **IP addresses are never features** — a model that
learns "10.4.7.3 is malicious" has memorised the lab, not learned detection; the
addresses travel with the record purely as alert metadata.  **Payload content is
never a feature** — see §7.

## 5. Scaling

**Within a box.** Every stage takes `--workers N`, which starts N processes in
one consumer group.  Redis hands each batch to exactly one member, so scaling is
a number, not a partitioning scheme.  Processes rather than threads because the
work is CPU-bound Python; the GIL cost is visible in `bench/inproc_bench.py`,
which reaches ~88k conn/s with threads versus 166–185k across processes.

**Across boxes.** Same mechanism: point more workers at the same Redis and they
join the same group.  The bus becomes the limit long before the workers do.

**Sharding, when one Redis is not enough.** Shard on `hash(flow_id) % N` into N
stream sets (`cn:conns:0..N-1`), each with its own ingest and scorer group.
Flow id is the right shard key because all records for one connection must reach
one consumer for `--aggregate` to fold them correctly.  Do not shard on source
IP: a single scanning host would hot-spot one shard, which is precisely when you
need the capacity.

**Concurrency guidance.**
* CPU-bound stages (ingest, scorer): multiprocessing, one worker per core,
  `intra_op_num_threads=1` for ONNX Runtime.  Letting each of N worker processes
  spawn its own ORT thread pool oversubscribes the machine and *lowers*
  aggregate throughput — this is the most common way to make this slower.
* I/O-bound stage (the API): asyncio.  The alert pump does its blocking bus read
  in an executor so the event loop keeps serving WebSockets.
* CPU affinity: `CN_CPU_AFFINITY="2,3,4,5"` pins worker *i* to the *i*-th listed
  CPU (Linux; a documented no-op on macOS).  Worth doing when the sensor shares
  a box with other latency-sensitive work; leave the first couple of cores for
  interrupt handling and the capture process.
* Batch size is the latency/throughput dial: `CN_PUBLISH_BATCH=512` gives ~7 ms
  p50 end-to-end.  Smaller batches cut latency and raise per-record overhead.

## 6. Reliability

* **At-least-once, explicitly.** Workers ack only after a batch is processed;
  `XAUTOCLAIM` reclaims anything a dead worker left pending after 30 s.  A crash
  costs a duplicate batch, not a lost one — the right trade when the output is
  alerts.
* **Bounded memory everywhere.** Streams trim at `CN_STREAM_MAXLEN`, the flow
  table caps at `CN_MAX_FLOWS` and force-expires the least recently seen flows,
  the API keeps a fixed-size alert ring.  An unbounded flow table is the classic
  way a sensor dies during the scan it was meant to detect.
* **Degradation is visible, not silent.** The benchmark reports max backlog and
  whether it stayed bounded; a run that keeps up and a run that buffers are
  reported differently even when both "processed" the same count.
* **A bad batch does not kill a stage.** `StageRunner` logs, counts and drops.

## 7. Security and privacy

* **Payloads are never retained by default.** `CN_STORE_PAYLOADS=0` is the
  default, and the feature set is built so that no feature *requires* payload
  content: `payload_entropy` is the entropy of the packet-size distribution, not
  of payload bytes.
* **Inspection ≠ retention.** The capture prototype does read up to 512 bytes of
  payload in memory to extract two pieces of metadata: whether a request looks
  like HTTP, and the *length* (never the value) of the TLS SNI.  Those bytes are
  discarded with the packet.  Opting in with `CN_STORE_PAYLOADS=1` logs a warning
  at startup, because it changes what the system is.
* **Address pseudonymisation.** `CN_ANONYMIZE_IPS=1` replaces addresses with a
  keyed BLAKE2b pseudonym in `100.64.0.0/10`, which preserves the ability to
  correlate a host's flows while removing the identity.  Since addresses are not
  features, this costs no detection quality at all.
* **Least privilege.** Only the capture process needs `CAP_NET_RAW`; ingest,
  scoring and the API run unprivileged, and the container image runs as a
  non-root user.

## 8. Known limitations

* **Detection is measured against a simulator.** The traffic model is calibrated
  to the shape of public IDS datasets, but it is a model.  BENCHMARK.md §9 sets
  out exactly what the number does and does not establish, and
  `model/train.py --dataset unsw|cic` runs the same pipeline on real data.
* **Per-flow verdicts, not campaign-level ones.** A 1.3% per-flow false-positive
  rate is ~130 alerts/s at 10k conn/s, which no analyst can triage.  The API's
  `/alerts/summary` rolls up by source, which is a triage aid, not a fix.  The
  real fix is a second stage (§9).
* **The capture prototype is ~10x too slow for a saturated gigabit link**
  (measured at 10.5k packets/s).
  Deliberate, documented, and the only stage that needs replacing.
* **The model is static.** No drift detection, no retraining loop, no feedback
  from analyst dispositions.
* **IPv6 flows are captured and scored, but the traffic model only generates
  IPv4**, so IPv6-specific behaviour is untested.

## 9. Production hardening — what to do next, in order

1. **Replace the capture prototype.** Zeek `conn.log` first (cheapest, biggest
   win), AF_PACKET fanout or eBPF if the link is busier.  See `capture/NOTES.md`.
2. **Add a correlation stage between scorer and alerting.** Aggregate flow
   verdicts by `(src_ip, window)` and alert on *campaigns*: 200 flagged flows
   from one host in 10 s is one port-scan alert with 200 pieces of evidence.
   This is where the per-flow false-positive rate stops mattering, and it is the
   single highest-value addition to this system.
3. **Persist alerts.** The stream is memory-bounded; alerts belong in something
   durable (Postgres, OpenSearch) with the flow record attached for forensics.
4. **Close the feedback loop.** Record analyst dispositions on alerts, use them
   as labels, retrain on a schedule, and gate deployment on the same acceptance
   criteria the benchmark checks.
5. **Detect drift in production.** Population Stability Index on the feature
   distributions versus the training set, alerting when the traffic stops
   looking like what the model was trained on.  The drift knob in the traffic
   model exists to test that this alarm works.
6. **Kafka** if retention, replay or multi-consumer requirements appear (§3).
7. **GPU inference** only if the model grows.  At 4.8M rows/s on one CPU core
   the current model has ~480x headroom over the 10k/s requirement; a GPU would
   add PCIe latency and a scheduling dependency to solve a problem that does not
   exist yet.  It becomes relevant with a sequence model over packet histories.
8. **Operational hardening**: TLS and authentication on the API (it is
   unauthenticated by design for the demo), Redis AUTH, resource limits per
   worker, and an alert-rate circuit breaker so a traffic anomaly cannot flood
   downstream systems.
