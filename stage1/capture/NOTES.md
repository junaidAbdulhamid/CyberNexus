# Capture: prototype vs production

`pcap_capture.py` is the correctness reference, not the production sensor.  It
uses scapy because scapy parses everything, replays a pcap without privileges,
and makes the flow-assembly logic testable in CI.  It is also, by a wide margin,
the slowest part of this system.

## What the prototype actually costs

Measured on the development box (Apple M4, Python 3.12, scapy 2.7), replaying a
24,000-packet / 4,000-connection capture through
`python -m capture.pcap_capture --pcap file.pcap` (3 runs):

| stage | throughput |
|---|---|
| scapy parse + flow update + publish | 10.3–10.9k packets/s → ~1.8k connections/s |
| flow table alone (no scapy) | >1M packets/s |

The flow table is not the problem.  Per-packet Python object construction is:
scapy builds a full layered object per packet, and that dominates everything
else by two orders of magnitude.  A 1 Gbit/s link at average packet sizes runs
roughly 100k–150k packets/s, so the scapy path is about 10x short of a single
saturated gigabit link and is only suitable for replay, lab traffic, and
correctness testing.

Everything downstream of capture — the bus, feature extraction, inference — is
already measured at >180k connections/s end-to-end (see `../BENCHMARK.md`), so
capture is the only stage that needs replacing to go to line rate.

## Production options, cheapest change first

### 1. Zeek as the exporter (recommended first move)

Run Zeek and ship `conn.log` (plus `ssl.log`/`http.log` for the TLS/HTTP
metadata) into the connection stream.  Zeek already does flow assembly, protocol
identification and TLS/HTTP parsing in C++, and its `conn.log` fields map almost
one-to-one onto `cybernexus.records.FIELDS`:

| Zeek field | CyberNexus field |
|---|---|
| `uid` | `flow_id` |
| `id.orig_h` / `id.resp_h` | `src_ip` / `dst_ip` |
| `id.orig_p` / `id.resp_p` | `src_port` / `dst_port` |
| `duration` | `duration` |
| `orig_bytes` / `resp_bytes` | `orig_bytes` / `resp_bytes` |
| `orig_pkts` / `resp_pkts` | `orig_pkts` / `resp_pkts` |
| `history` | `syn/ack/fin/rst/psh/urg` counts (parse the flag string) |
| `ssl.log server_name` | `tls_sni_len` (length only) |

What Zeek does not give you directly: per-flow inter-arrival and packet-size
moments.  Either add a small Zeek script that accumulates them, or accept the
loss of those features and retrain — `model/train.py` reports the cost honestly
because the features simply carry no signal.

Trade-off: one more daemon to operate, and Zeek's own capture path still needs
AF_PACKET fanout or PF_RING to keep up on a busy link.

### 2. AF_PACKET with fanout (Linux, moderate effort)

A capture process per core, all bound to the same `PACKET_FANOUT_HASH` group, so
the kernel hashes each flow to a fixed worker and a connection is never split
across processes.  This is the standard way Suricata and Zeek scale on commodity
NICs and reaches multi-gigabit rates with no special hardware.  In this
codebase, only `scapy_to_meta` and the source loop change — `FlowTable` and
everything after it are untouched.

### 3. eBPF / XDP (Linux, higher effort, best throughput per watt)

Do the flow accounting *in the kernel*: an XDP program keyed on the 5-tuple
updates a `BPF_MAP_TYPE_LRU_HASH` of counters, and user space drains the map on
a timer instead of seeing individual packets.  Packets never cross into user
space, which removes the entire per-packet cost this prototype is dominated by.

This maps unusually well onto the existing design because `FlowState` is already
a fixed-size struct of counters and running moments — exactly what fits in a BPF
map value.  The user-space drain would emit the same record tuples, so ingest,
the model and the API do not change at all.

Caveats: kernel version constraints, verifier limits on the parsing you can do
(TLS SNI extraction is awkward in XDP), and no easy way to do reassembly.

### 4. DPDK / AF_XDP with a Rust or C exporter (10 GbE+)

Kernel bypass with poll-mode drivers, hugepages and a core pinned per queue.
This is what line-rate 10/40/100 GbE capture looks like in practice, and at that
point the exporter should be a separate Rust or C binary that writes the same
msgpack record batches to the bus.  The wire format in `cybernexus/records.py`
is deliberately language-neutral (an array of scalars, fixed order) so a Rust
exporter needs `rmp-serde` and about a page of code, with no shared Python.

Costs: dedicated NIC and cores, no other traffic on that interface, and a much
heavier operational story.  Do not go here before measuring that option 2 or 3
is genuinely insufficient.

## Sampling and back-pressure

Whatever the capture path, decide what happens when the pipeline cannot keep up:

* **Bounded stream, drop oldest** (current default): `CN_STREAM_MAXLEN` trims the
  connection stream.  Detection degrades gracefully, and the backlog metric in
  the benchmark report tells you it is happening.
* **Flow sampling**: drop a deterministic hash-based fraction of *flows* (never
  packets — half a flow is worse than no flow).
* **Prioritised admission**: always keep flows to/from external addresses and
  flows on rarely-seen ports; sample the bulk internal chatter.

The one thing not to do is silently sample packets, which corrupts every
per-flow statistic the model depends on.
