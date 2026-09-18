# CyberNexus — Stage 2: live 3D network map

A real-time 3D map of the network that takes [Stage 1](../stage1) detections and
lights up the hosts behind them. Discovery builds the device graph, a connector
correlates each alert to a device, and the browser renders the result at 60 fps
with the affected host impossible to miss.

| | measured on a 10-core laptop |
|---|---|
| Alert injection → host marked on screen | **101 ms median**, 771 ms p95 |
| Frame rate, 428 nodes, incident in progress | **60 fps**, 3–7 draw calls |
| Topology payload | 100 KB compact (3.1× smaller than the object form) |
| Identification time vs. a log console | **46.7% faster** (45–55% across runs) — a *model* output, see below |
| Tests | 135 Python, 21 browser (integration, accessibility, visual regression) |

That last figure clears the brief's 40% MTTI target, and it comes with a caveat stated
up front rather than in a footnote: **no humans were timed.** The harness
measures how the two interfaces behave and applies a published HCI model to
predict human time. [MTTI.md](MTTI.md) sets out exactly what is measured, what is
modelled, where the model breaks, and the protocol for running the real study.

---

## Quickstart

### Docker (nothing to install)

```bash
docker compose up --build
open http://localhost:8080          # API key: demo-key
```

Brings up Redis, generates a 428-node example network, starts the backend with
the built frontend, and publishes synthetic Stage 1 alerts so the map has
something to show. First run takes a couple of minutes, mostly the frontend
build.

### Local

```bash
make setup                 # python deps + npm install for frontend and tests
make topology              # generate the example network
make frontend              # build the UI
make redis-up              # Redis in Docker (or bring your own)
make backend               # http://localhost:8080
```

In another terminal, give it some traffic to show:

```bash
PYTHONPATH=. ../.venv/bin/python -m integration.simulator --incident-every 20
```

For frontend development with hot reload, run `cd frontend && npm run dev` and
open <http://localhost:5173>; Vite proxies `/api` and `/ws` to the backend.

### Driving it from a real Stage 1

Both stages talk to the same Redis stream, so nothing needs configuring beyond
pointing them at the same instance:

```bash
docker compose stop simulator          # stop the synthetic alerts
cd ../stage1 && docker compose up      # real capture -> detection -> cn:alerts
```

Stage 2 consumes `cn:alerts` as a consumer group, so it competes with nothing and
several backend replicas can share the load.

---

## What you are looking at

The network is laid out as a **layered city**. Each subnet is a district on the
ground plane, labelled with the department that owns it; switches sit above their
district and routers above those, so "up" means "closer to the core". Positions
are deterministic — the map looks the same tomorrow, which is what lets an
operator learn it.

When a host alerts, it changes in **three redundant ways at once**: colour, size,
and a pulsing halo. The rest of the network dims. The worst incident names
itself in a banner at the top — host, address, behaviour — so identifying it
needs no clicking at all.

**Keyboard** (press `?` for the full list): `w` jump to worst, `n`/`p` walk the
alert list, `Enter` fly to it, `a` acknowledge, `/` search, `c` cycle palette,
`b` switch to the log view.

---

## Running discovery for real

The example topology is synthetic. To map an actual network:

```bash
# passive only — reads this host's ARP cache and LLDP neighbours, sends nothing
PYTHONPATH=. python -m discovery.run_discovery --out data/topology.json

# add SNMP against known infrastructure (one walk of a router returns its whole
# ARP table, which maps every host in the subnets it serves)
python -m discovery.run_discovery --snmp 10.0.0.1,10.0.0.2 --community public

# add an ARP sweep and reverse DNS (sweeping needs root)
sudo -E python -m discovery.run_discovery --sweep 10.0.1.0/24 --resolve

# parse captured output instead of probing anything
python -m discovery.run_discovery --arp-file arp.txt --lldp-file lldp.json
```

Then `CN_MAP_TOPOLOGY=data/topology.json make backend`.

Active probing is opt-in, one flag per technique, and every probe degrades
gracefully: no scapy, no root, or no `snmpwalk` means fewer results, never a
crash. Links discovered by LLDP or SNMP are marked `ethernet`/`uplink`; hosts
known only from an ARP table are attached to their subnet's switch and marked
`inferred`, because ARP proves a host is in the broadcast domain, not which port
it is on.

---

## Layout

| directory | contents |
|---|---|
| `cnmap/` | shared library: domain model, deterministic 3D layout, live state store with severity decay, alert→device correlation |
| `discovery/` | ARP, LLDP and SNMP parsers, active probes, topology builder, example-network generator, CLI |
| `integration/` | Stage 1 alert codec, Redis consumer-group connector, alert simulator, ticketing webhook |
| `backend/` | FastAPI: REST snapshots, WebSocket streaming, auth stub, coalescing fan-out hub |
| `frontend/` | React + Three.js map, alert log baseline, accessibility layer |
| `ui-tests/` | Playwright integration, accessibility and visual-regression suites, plus the MTTI harness |
| `tests/` | Python tests for discovery, topology, correlation, store and API |

---

## API

All endpoints need `X-API-Key` (or `?api_key=` for the WebSocket), except
`/api/health`.

| endpoint | purpose |
|---|---|
| `GET /api/topology?format=compact` | topology snapshot, ETag-cacheable |
| `GET /api/nodes/{id}` | device detail, state, neighbours, recent alerts |
| `GET /api/states` | every currently-alerting node, worst first |
| `GET /api/alerts` | recent alerts, filterable by severity and node |
| `GET /api/timeline` · `/api/timeline/export` | history for the scrubber; CSV or JSON export |
| `GET /api/replay?ts=` | reconstructed state at an instant |
| `GET /api/unresolved` | alerts that matched no known device — an inventory gap |
| `POST /api/nodes/{id}/acknowledge` | clear a host's highlight (history is kept) |
| `POST /api/nodes/{id}/ticket` | raise a ticket through the webhook stub |
| `POST /api/simulate/incident` | inject a synthetic incident (demo and harness) |
| `WS /ws/stream` | live node-state deltas |

---

## Configuration

| variable | default | meaning |
|---|---|---|
| `CN_MAP_TOPOLOGY` | `data/example-topology.json` | topology to serve |
| `CN_MAP_REDIS_URL` | `redis://localhost:6379/0` | Stage 1 stream and shared state |
| `CN_ALERT_STREAM` | `cn:alerts` | Stage 1 alert stream |
| `CN_MAP_ALERT_START` | `$` | `$` = live alerts only, `0` = replay the stream |
| `CN_MAP_API_KEY` | `demo-key` | API key for the auth stub |
| `CN_MAP_HALF_LIFE` | `120` | seconds for an alert score to halve |
| `CN_MAP_DWELL` | `30` | seconds a node holds its peak severity before decaying |
| `CN_MAP_CORRELATE_PREFER` | `internal` | which end of a flow is "the host" |
| `CN_MAP_WEBHOOK_URL` | unset | ticketing endpoint; unset means dry-run |
| `CN_MAP_ALLOW_SIMULATION` | `1` | set `0` to disable incident injection |
| `CN_MAP_AUTH` | `1` | set `0` only for local development |

---

## Tests

```bash
make test-py          # 135 python tests
make test-ui          # 21 browser tests (needs the backend running)
make test             # both
make snapshots        # regenerate visual-regression baselines
make mtti             # the MTTI experiment
```

The browser suite uses the Chrome already installed on the machine
(Playwright `channel: "chrome"`), so it needs no browser download. The visual
suite masks the 3D canvas — it animates continuously and renders through
whatever GPU is present, so pixel-comparing it would fail for reasons unrelated
to the change under review. Canvas rendering is covered instead by an assertion
that the pixels *change* when a node starts alerting.

---

## Known limitations

- **The MTTI number is a model output**, not a human measurement ([MTTI.md](MTTI.md)).
- **Authentication is a stub** — an API key and a JWT, no real identities, no
  RBAC enforcement, no audit trail.
- **Node state is per-replica.** Running several backends shares the alert load
  but not the live state, so two browsers on two replicas can briefly disagree.
  Run one backend, or pin sessions, until the pub/sub sync in
  [DESIGN.md §5](DESIGN.md#5-scaling) is implemented.
- **The example topology is synthetic.** Discovery works against real networks
  but has not been run against one at scale here.
- **Alerts and topology history are bounded in memory** — the timeline ring holds
  20,000 events by default and nothing is persisted beyond Redis.
- **IPv6 is parsed and correlated but the example network is IPv4 only**, so the
  v6 path is under-exercised.

---

## Documents

- [DESIGN.md](DESIGN.md) — why 3D is justified, how alerts map to devices, scaling, production roadmap
- [MTTI.md](MTTI.md) — the experiment: what is measured, what is modelled, and the human protocol
- [../stage1/README.md](../stage1/README.md) — the detection pipeline that feeds this
