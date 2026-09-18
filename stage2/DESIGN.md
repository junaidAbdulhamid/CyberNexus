# Design rationale

Why this is built the way it is, what each choice costs, and what would have to
change to run it on a real network. Measured results are in
[MTTI.md](MTTI.md); the reproduction instructions are in [README.md](README.md).

---

## 1. Is 3D justified at all?

Most 3D data visualisation is decoration. A third spatial dimension adds
occlusion, perspective distortion and a navigation burden, and usually buys
nothing a good 2D layout could not deliver. That has to be argued, not assumed.

What it buys here is **one screen for a network that does not fit on one
screen**. A 400-host network laid out in 2D is either a hairball or a tree so
wide it needs scrolling; in 3D the same network becomes a set of districts on a
plane with the routing spine rising above them, and the whole thing fits in one
view with the hierarchy legible. The vertical axis is not decorative — it
carries the access/distribution/core tier, so "up" consistently means "closer to
the core", and the height of an alerting node tells you immediately whether the
problem is a workstation or a router.

What it costs is honest to state: nodes occlude each other, distance judgements
are unreliable, and someone who cannot use a mouse cannot orbit a camera. Every
one of those costs is mitigated by something that is *not* the 3D view — the
ranked alert list, the incident banner, the keyboard path, the log view — and
the design treats the 3D scene as the fastest channel rather than the only one.

**The strongest evidence for the 3D view in this repository is also the least
flattering to it**: in the MTTI model, the map's advantage comes overwhelmingly
from pop-out detection and from showing one node instead of six log rows, not
from depth. A well-built 2D map with the same severity encoding would capture
most of the same benefit.

---

## 2. How an alert becomes a node

Stage 1 emits a verdict about a *flow* — a five-tuple and a score. The map needs
a *device*. `cnmap/correlate.py` bridges the two, and getting it wrong is worse
than useless: an alert pinned to the wrong host sends someone to the wrong desk.

Resolution runs most-certain-first:

1. **exact IP** — the address is in the topology's address index;
2. **MAC** — when the alert carries one (Stage 1 does not today; the hook exists
   because a Zeek or DHCP-aware exporter would);
3. **asset tag** — an external CMDB identifier on the alert;
4. **subnet** — no host matched, but the address is inside a known subnet, so the
   alert attaches to that subnet's gateway and is labelled `subnet` so the UI can
   show it as approximate;
5. **unresolved** — counted and exposed at `/api/unresolved`, never dropped.

That last rule matters. An alert the map cannot place is an **inventory gap**:
there is a device on the network that discovery has not seen. Silently
discarding it would hide exactly the thing a security team most needs to know.

**Which end of the flow is the host in trouble?** The internal endpoint, and
when both ends are internal, the initiator. That is a judgement call, so it is
written down and configurable (`CN_MAP_CORRELATE_PREFER`) rather than buried.

**Severity** is derived from Stage 1's score, with cut points that are policy,
not physics: Stage 1's calibrated threshold is ~0.22, so anything below that is
not an alert at all, and the bands widen as confidence rises.

---

## 3. The UI choices that make identification fast

Each of these was either measured to matter or added because a measurement said
it was missing.

**Pop-out, three times over.** An alerting node differs from its neighbours in
colour, in size, and in motion. Any one of those is a pre-attentive feature that
the visual system finds in roughly constant time regardless of how many other
nodes are present. Three redundant channels means the effect survives a
colour-vision deficiency, a greyscale projector, and a viewer across the room.

**Pop-out is rationed.** With a noisy sensor, dozens of nodes can be marked at
once — and if every one pulses, none stands out. The halo-and-pulse treatment is
reserved for the most severe tier currently present and capped at six nodes.
Nothing is hidden; only the *animation budget* is rationed. This was added after
a benchmark run showed 245 nodes pulsing simultaneously.

**Everything else dims.** When any node is alerting, the rest of the network
drops to 55% brightness. Not further: the operator still needs to see which
district the hot node is in.

**Alerting nodes ignore filters and level-of-detail.** A filter that would hide
an alerting node does not, and a zoom level that collapses its district into a
cluster still draws it individually. A visualisation that can hide the thing you
are looking for is worse than a list.

**The layout never moves.** Positions are computed server-side, deterministically
from node identity, and shipped with the topology. A force-directed graph that
settles differently on every page load destroys the one thing that makes a map
fast to read: an operator who has learned that "finance is the cluster on the
left" keeps that knowledge between shifts and across screens.

**Districts are named by department, not by CIDR.** "finance" is what an
operator is told on the phone; "10.20.10.0/24" is what they then have to look up.

**The banner answers the question.** The worst active incident names itself —
host, address, behaviour, and the three actions worth taking — with no
interaction. This exists because the MTTI run showed the map spending most of
its modelled time on a pointer movement and a click made purely to learn the
host's name. The map had already solved the hard part and was making the
operator work for the easy part.

**Severity holds before it decays.** A node that hit critical stays critical for
a dwell period (default 30 s) before its score starts decaying. Without it, a
node scoring 0.96 crosses the critical boundary within 200 ms and the map
downgrades an incident while the operator is still turning to look at it.

**Alerts cool down on their own.** After the dwell, severity decays with a
configurable half-life. A map where everything that ever alerted stays red is a
map that is red.

---

## 4. Architecture

```
 discovery ──► topology.json ──► backend ──REST snapshot──► browser ──► Three.js scene
 (ARP/LLDP/SNMP)                    │                          ▲
                                    │                          │
 Stage 1 ──► cn:alerts ──► connector ──► correlate ──► store ──┴──WebSocket deltas
 (Redis Streams)                                        │
                                                        └──► webhook / ticketing
```

**Split by change rate.** The topology is large and nearly static, so it is a
cacheable REST snapshot with an ETag. Node state is tiny and changes constantly,
so it streams. A client fetches the snapshot once and then receives only deltas.

**Compact wire format.** The topology ships as columnar arrays with links
referencing nodes by index — 3.1× smaller than the object form on a 428-node map,
and most of the saving is not repeating long node ids on every link.

**Updates coalesce twice.** The hub batches whatever accumulated in the last
50 ms into a single WebSocket frame, and the client's store notifies React at
10 Hz while pushing straight into the 3D scene synchronously. During an incident
one node can produce dozens of alerts a second; without coalescing the browser
spends the frame budget in React reconciling a list nobody is reading.

**One draw call per object class.** Nodes are a single `InstancedMesh`, links a
single `LineSegments`, halos a second `InstancedMesh`. 428 nodes render in 3–7
draw calls at 60 fps; 5,000 would cost the same. Instance buffers are rebuilt
only on structural change — topology, filter, alert state, LOD level — and at
most once per frame. The per-frame work is limited to pulsing the handful of
nodes that are actually alerting.

### Technology choices

| choice | why | what it costs |
|---|---|---|
| **React + Three.js (WebGL2)** | Three.js has instancing, a mature scene graph and works everywhere; React handles the panels, which are ordinary UI | Three.js is ~490 kB, split into its own chunk so it caches across releases |
| **React does not drive the render loop** | the scene runs its own rAF loop and is mutated imperatively | two state models to keep in step, bridged in one place (`MapView`) |
| **FastAPI** | same language and model as Stage 1, native WebSockets, and pydantic models *are* the API schema, so frontend and backend cannot drift | Python's concurrency ceiling; see §5 |
| **Redis** | Stage 1 already publishes to Redis Streams, so the connector is a consumer group on an existing dependency | not a graph database; see §6 |
| **WebSocket + REST** | REST for the big static thing, WebSocket for the small live thing | two transports to secure instead of one |

**WebGPU**, not used: it would allow compute-shader layout and cheaper instancing,
but browser support is still uneven and the current bottleneck is not the GPU —
a 428-node map runs at 60 fps with ~36k triangles and seven draw calls. The
migration path is contained to `NetworkScene`, since nothing above it touches
WebGL directly.

---

## 5. Scaling

**Within a process.** Instancing means node count barely affects render cost.
The practical limits are the topology payload (mitigated by the compact format
and ETag caching) and the flow of updates (mitigated by coalescing).

**Level of detail.** Subnets collapse into cluster spheres when the camera is
further out than 2.6× the scene radius, or past 1.4× for topologies over 1,500
nodes. The thresholds are relative to the scene's own size, not absolute world
units — an absolute threshold collapses a large network the moment it loads
(its bounds are bigger) while never triggering on a small one.

**Across backend replicas.** The connector is a Redis consumer-group member, so
running N backends shares the alert load automatically. What they do *not* yet
share is node state: each replica keeps its own in-memory copy, persisted to
Redis but not invalidated across replicas, so two browsers on two replicas can
briefly disagree. The fix is a Redis pub/sub channel for state deltas — the
`MapStore` persistence hooks are already in place for it. Until then, run one
backend or pin sessions to a replica.

**Sharding by region.** For a multi-site network, shard the topology per site:
one backend per site with its own Redis, and a thin aggregator serving a
site-level overview that drills into the per-site map. Shard on site rather than
on subnet, because an incident is investigated by the team that owns the site.

**Where Python runs out.** The connector handles Stage 1's full alert rate
comfortably (Stage 1 sustains ~10k conn/s and alerts are a small fraction of
that), because alerts arrive in batches and correlation is a dict lookup. The
ceiling is the WebSocket fan-out: one process serving hundreds of browsers each
receiving every update. At that point, move fan-out to a dedicated broker.

---

## 6. Data store

Redis holds the demo's live state. It is not a graph database, and for the
queries this UI makes — "give me every node", "give me one node's neighbours" —
it does not need to be, because the whole topology fits in memory and is served
as a snapshot.

**When to move to Neo4j or JanusGraph**: when the questions become graph
questions. "What is the blast radius of this compromised host at two hops?"
"Which paths exist from the guest VLAN to the cardholder data environment?"
"What changed in the topology between Tuesday and today?" Those are traversals
and temporal queries, and doing them over a JSON snapshot means reimplementing a
graph engine badly. The port is contained: `MapStore` is the only component that
owns topology state, and `Topology`/`Node`/`Link` map onto a property graph
almost directly.

---

## 7. Accessibility

A 3D canvas is opaque to assistive technology, so the visualisation is never the
only channel:

- **a live region announces alerts** — `role="alert"` (assertive) for critical
  and high, `role="status"` (polite) below that, so a screen-reader user learns
  about an incident at the same moment a sighted one sees the node flash;
- **a visually hidden, fully focusable list** mirrors the alerting nodes, so Tab
  order reaches every highlighted host; focusing an item makes it visible;
- **complete keyboard control** — `w` jumps to the worst alert, `n`/`p` walk the
  ranked list, `Enter` flies the camera, `a` acknowledges, `/` searches, `b`
  switches to the log view, `?` lists the lot;
- **three palettes** — the default (Okabe-Ito derived), a colourblind-safe
  luminance ramp where ordering is carried by lightness rather than hue, and a
  high-contrast mode on pure black;
- **severity never depends on colour alone** — size, halo, pulse rate, a glyph
  (`!` to `!!!!`) and a text label all encode it;
- `prefers-reduced-motion` disables the pulse animation.

---

## 8. Security and privacy

- **No payload data, ever.** Stage 1 never puts packet contents on the wire and
  Stage 2 never asks for them. What flows to the browser is topology metadata and
  alert metadata — five-tuples, scores, timestamps. A test asserts the node-detail
  payload contains no field resembling packet content.
- **Authentication is a stub, and says so.** An API key or a JWT minted from it;
  `/api/health` reports `"demo stub, not production identity"`. There are no
  users, no revocation, no audit trail. Production needs OIDC against the
  organisation's IdP, RBAC scoped per site and subnet, and per-action audit. The
  role-checking dependency (`require_role`) is wired into every mutating endpoint
  already, so the call sites are correct even though the identities are not real.
- **Active discovery is opt-in**, one flag per technique. On someone else's
  network an ARP sweep is something you ask permission for before you write code
  for it.
- **The webhook stub carries asset and alert metadata only**, and logs exactly
  what it would have sent in dry-run mode so it is reviewable before it is armed.

---

## 9. Production hardening — in order

1. **Replace the synthetic topology with real discovery.**
   `discovery/run_discovery.py` runs today against ARP, LLDP and SNMP; what it
   needs for production is scheduling, change detection (what appeared, what
   vanished) and a merge policy for conflicting observations.
2. **Share node state across replicas** via Redis pub/sub, so horizontal scaling
   does not produce disagreeing browsers (§5).
3. **Real identity**: OIDC, RBAC per site/subnet, audit log on acknowledge and
   ticket actions.
4. **Persist alerts and topology history** — the stream and the timeline ring are
   both bounded. An incident review a week later needs a durable store, which is
   also what makes topology diffing possible.
5. **Graph database** when blast-radius and path queries appear (§6).
6. **Server-side clustering** for networks beyond ~20k nodes, so the client
   receives districts and expands on demand rather than holding the whole
   topology.
7. **WebGPU** when browser support settles, for compute-shader layout of very
   large graphs.
8. **Close the loop with Stage 1**: feed analyst dispositions (acknowledged as
   false positive / confirmed) back as labels for retraining. Stage 1's
   `DESIGN.md` lists the same item from its side; this is the interface where it
   would attach.
