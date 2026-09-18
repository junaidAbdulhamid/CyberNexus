"""Backend: topology snapshots over REST, live node state over WebSocket.

Split by change rate.  The topology is large and nearly static, so it is a
cacheable REST snapshot with an ETag.  Node state is tiny and changes constantly,
so it streams.  A client fetches the snapshot once and then only ever receives
deltas — which is what keeps a 5,000-node map responsive on an incident.

Route map:

    GET  /api/health                    liveness, connector status, versions
    POST /api/auth/token                exchange the API key for a JWT
    GET  /api/topology                  snapshot (?format=compact for the wire)
    GET  /api/topology/stats            counts by type/subnet/vendor
    POST /api/topology/reload           re-read the topology file
    GET  /api/nodes/{id}                node detail + recent alerts + state
    GET  /api/states                    every currently-alerting node
    GET  /api/alerts                    recent alerts, filterable
    GET  /api/timeline                  history for the scrubber
    GET  /api/timeline/export           CSV or JSON export
    GET  /api/replay?ts=                reconstructed state at an instant
    POST /api/nodes/{id}/acknowledge    clear a node's highlight
    POST /api/nodes/{id}/ticket         raise a ticket via the webhook stub
    GET  /api/integrations/webhook      recent delivery log
    GET  /api/unresolved                alerts that matched no known device
    POST /api/simulate/incident         inject a synthetic incident (demo)
    WS   /ws/stream                     live node-state updates
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from cnmap.config import settings
from cnmap.layout import bounds
from cnmap.models import Severity, Topology
from cnmap.store import MapStore, export_timeline_csv
from integration.connector import Stage1Connector
from integration.webhook import WebhookSender, build_ticket_payload

from .auth import authenticate, authenticate_ws, issue_token, require_role
from .hub import Hub

log = logging.getLogger("cnmap.backend")

# Replaced during startup with the real topology. Initialised here rather than
# left as a bare annotation so the module is importable and usable (empty) even
# when the lifespan handler has not run - which is what the test client and any
# tooling that merely imports the app both do.
store: MapStore = MapStore()
hub = Hub()
connector: Optional[Stage1Connector] = None
webhook = WebhookSender(url=settings.webhook_url, token=settings.webhook_token,
                        dry_run=not settings.webhook_url)
_redis = None
_simulator = None
_started_at = time.time()


def load_topology() -> Topology:
    path = Path(settings.topology_path)
    if path.exists():
        return Topology.model_validate_json(path.read_text())
    log.warning("no topology at %s; generating a synthetic one", path)
    from discovery.synthesize import synthesize

    return synthesize(200, seed=7)


def connect_redis():
    if not settings.use_redis:
        return None
    try:
        import redis as redis_lib

        client = redis_lib.Redis.from_url(settings.redis_url)
        client.ping()
        log.info("connected to redis at %s", settings.redis_url)
        return client
    except Exception as exc:
        log.warning("redis unavailable (%s); running with in-process state only", exc)
        return None


async def decay_loop() -> None:
    """Cool nodes down over time and broadcast only actual changes."""
    while True:
        await asyncio.sleep(settings.decay_interval_s)
        try:
            for update in store.decay():
                await hub.publish(json.loads(update.model_dump_json()))
        except Exception as exc:  # pragma: no cover
            log.warning("decay loop: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global store, connector, _redis, _simulator
    logging.basicConfig(level=settings.log_level.upper(),
                        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    _redis = connect_redis()
    topology = load_topology()
    store = MapStore(topology, half_life_s=settings.half_life_s,
                     dwell_s=settings.dwell_s, timeline_size=settings.timeline_size,
                     redis_client=_redis, namespace=settings.namespace)
    store.correlator.prefer = settings.correlate_prefer
    log.info("topology loaded: %d nodes, %d links", len(topology.nodes), len(topology.links))

    from integration.simulator import AlertSimulator

    _simulator = AlertSimulator(topology, seed=4242)

    hub.bind_loop(asyncio.get_running_loop())
    flusher = asyncio.create_task(hub.flush_loop())
    decayer = asyncio.create_task(decay_loop())

    if settings.connector_enabled and _redis is not None:
        connector = Stage1Connector(
            store, _redis, stream=settings.alert_stream, group=settings.alert_group,
            consumer=f"cnmap-{int(time.time())}",
            on_update=lambda update: _on_update(update),
            start_id=settings.alert_start_id,
        )
        connector.start()
        log.info("Stage 1 connector started on %s", settings.alert_stream)
    else:
        log.warning("Stage 1 connector disabled (redis=%s, enabled=%s)",
                    _redis is not None, settings.connector_enabled)

    try:
        yield
    finally:
        flusher.cancel()
        decayer.cancel()
        if connector:
            connector.stop()


def _on_update(update) -> None:
    """Connector thread -> event loop.  Also fires the auto-ticket rule."""
    hub.publish_threadsafe(json.loads(update.model_dump_json()))
    auto = settings.webhook_auto_severity
    if auto and update.state.severity.rank >= Severity(auto).rank and webhook.configured:
        node = store.get_topology().node_index().get(update.state.node_id)
        if node:
            webhook.send_async(build_ticket_payload(
                node, update.state, store.alerts_for_node(update.state.node_id, 20)))


app = FastAPI(title="CyberNexus Live Map", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origins or ["*"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health() -> dict:
    return {
        "status": "ok",
        "uptime_s": time.time() - _started_at,
        "topology_version": store.get_topology().version,
        "nodes": len(store.get_topology().nodes),
        "redis": _redis is not None,
        "connector": connector.status() if connector else {"connected": False, "running": False},
        "hub": hub.stats(),
        "auth": {
            "enabled": settings.auth_enabled,
            "mode": "api-key + JWT (demo stub, not production identity)",
        },
        "webhook": {"configured": bool(settings.webhook_url), "dry_run": webhook.dry_run},
    }


@app.post("/api/auth/token")
async def token(request: Request) -> dict:
    """Exchange the API key for a short-lived JWT."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    key = request.headers.get("x-api-key") or body.get("api_key", "")
    if settings.auth_enabled and key != settings.api_key:
        raise HTTPException(401, "invalid api key")
    return issue_token(subject=body.get("subject", "demo"), role=body.get("role", "analyst"))


# ---------------------------------------------------------------------------
# topology
# ---------------------------------------------------------------------------

COMPACT_NODE_FIELDS = ("id", "name", "ip", "mac", "vendor", "device_type", "os",
                       "subnet", "site", "criticality", "layer", "x", "y", "z", "tags")
COMPACT_LINK_FIELDS = ("source", "target", "kind")


def compact_topology(topology: Topology) -> dict:
    """Columnar form: ~5x smaller than the object form on a 400-node map.

    Links reference nodes by *index*, which is where most of the saving comes
    from — node ids are long and every link repeats two of them.
    """
    index = {node.id: i for i, node in enumerate(topology.nodes)}
    nodes = [
        [n.id, n.name, n.ip or "", n.mac or "", n.vendor, n.device_type.value, n.os,
         n.subnet, n.site, n.criticality, n.layer,
         round(n.position.x, 3), round(n.position.y, 3), round(n.position.z, 3), n.tags]
        for n in topology.nodes
    ]
    links = [
        [index[l.source], index[l.target], l.kind]
        for l in topology.links if l.source in index and l.target in index
    ]
    return {
        "format": "compact",
        "version": topology.version,
        "generated_at": topology.generated_at,
        "source": topology.source,
        "node_fields": list(COMPACT_NODE_FIELDS),
        "link_fields": list(COMPACT_LINK_FIELDS),
        "nodes": nodes,
        "links": links,
        "bounds": bounds(topology.nodes),
        "stats": topology.stats(),
    }


@app.get("/api/topology")
async def get_topology(
    request: Request,
    format: str = Query("compact", pattern="^(compact|full)$"),
    principal: dict = Depends(authenticate),
):
    topology = store.get_topology()
    etag = f'W/"topology-{topology.version}-{format}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    payload = compact_topology(topology) if format == "compact" else json.loads(
        topology.model_dump_json()
    )
    return JSONResponse(payload, headers={"ETag": etag, "Cache-Control": "no-cache"})


@app.get("/api/topology/stats")
async def topology_stats(principal: dict = Depends(authenticate)) -> dict:
    topology = store.get_topology()
    return {**topology.stats(), "bounds": bounds(topology.nodes),
            "version": topology.version, "source": topology.source}


@app.post("/api/topology/reload")
async def reload_topology(principal: dict = Depends(require_role("analyst", "admin"))) -> dict:
    topology = store.set_topology(load_topology())
    await hub.broadcast_now({"type": "topology_changed", "version": topology.version})
    return {"reloaded": True, "version": topology.version, "nodes": len(topology.nodes)}


# ---------------------------------------------------------------------------
# nodes and state
# ---------------------------------------------------------------------------

@app.get("/api/nodes/{node_id}")
async def get_node(node_id: str, alert_limit: int = Query(25, ge=1, le=200),
                   principal: dict = Depends(authenticate)) -> dict:
    topology = store.get_topology()
    node = topology.node_index().get(node_id)
    if node is None:
        raise HTTPException(404, f"no such node: {node_id}")
    neighbours = []
    for link in topology.links:
        if link.source == node_id:
            neighbours.append({"id": link.target, "kind": link.kind})
        elif link.target == node_id:
            neighbours.append({"id": link.source, "kind": link.kind})
    state = store.get_state(node_id)
    alerts = store.alerts_for_node(node_id, alert_limit)
    return {
        "node": json.loads(node.model_dump_json()),
        "state": json.loads(state.model_dump_json()) if state else None,
        "neighbours": neighbours,
        "alerts": [json.loads(a.model_dump_json()) for a in alerts],
        "telemetry": {
            "alert_count": state.alert_count if state else 0,
            "alert_count_window": state.alert_count_window if state else 0,
            "top_ports": state.top_ports if state else [],
            "top_peers": state.top_peers if state else [],
            "first_seen": node.first_seen, "last_seen": node.last_seen,
            "services": node.services, "discovered_by": node.discovered_by,
        },
    }


@app.get("/api/states")
async def get_states(principal: dict = Depends(authenticate)) -> dict:
    states = store.get_states()
    return {"ts": time.time(), "count": len(states),
            "states": [json.loads(s.model_dump_json()) for s in states]}


@app.post("/api/nodes/{node_id}/acknowledge")
async def acknowledge(node_id: str, principal: dict = Depends(require_role("analyst", "admin"))) -> dict:
    state = store.acknowledge(node_id)
    if state is None:
        raise HTTPException(404, f"node {node_id} has no active state")
    # severity is now "none", which every client treats as "stop highlighting".
    await hub.publish({"type": "node_state", "state": json.loads(state.model_dump_json()),
                       "seq": store.seq})
    return {"acknowledged": True, "node_id": node_id, "by": principal.get("sub")}


# ---------------------------------------------------------------------------
# alerts, timeline, replay
# ---------------------------------------------------------------------------

@app.get("/api/alerts")
async def get_alerts(limit: int = Query(100, ge=1, le=2000),
                     min_severity: Severity = Query(Severity.INFO),
                     node_id: Optional[str] = None,
                     principal: dict = Depends(authenticate)) -> dict:
    alerts = (store.alerts_for_node(node_id, limit) if node_id
              else store.recent_alerts(limit, min_severity))
    return {"count": len(alerts), "alerts": [json.loads(a.model_dump_json()) for a in alerts]}


@app.get("/api/timeline")
async def get_timeline(start: Optional[float] = None, end: Optional[float] = None,
                       limit: int = Query(5000, ge=1, le=20000),
                       principal: dict = Depends(authenticate)) -> dict:
    events = store.timeline_range(start, end, limit)
    span = {"start": events[0].ts, "end": events[-1].ts} if events else None
    return {"count": len(events), "span": span,
            "events": [json.loads(e.model_dump_json()) for e in events]}


@app.get("/api/timeline/export")
async def export_timeline(format: str = Query("csv", pattern="^(csv|json)$"),
                          start: Optional[float] = None, end: Optional[float] = None,
                          principal: dict = Depends(authenticate)):
    events = store.timeline_range(start, end)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    if format == "csv":
        return PlainTextResponse(
            export_timeline_csv(events), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="alert-timeline-{stamp}.csv"'},
        )
    return JSONResponse(
        {"exported_at": time.time(), "count": len(events),
         "events": [json.loads(e.model_dump_json()) for e in events]},
        headers={"Content-Disposition": f'attachment; filename="alert-timeline-{stamp}.json"'},
    )


@app.get("/api/replay")
async def replay(ts: float = Query(..., description="epoch seconds"),
                 principal: dict = Depends(authenticate)) -> dict:
    states = store.replay_at(ts)
    return {"ts": ts, "count": len(states),
            "states": [json.loads(s.model_dump_json()) for s in states]}


@app.get("/api/unresolved")
async def unresolved(principal: dict = Depends(authenticate)) -> dict:
    """Alerts that matched no known device — an inventory gap, surfaced."""
    return {"addresses": store.correlator.unresolved_report(50),
            "stats": store.correlator.stats()}


@app.get("/api/stats")
async def stats(principal: dict = Depends(authenticate)) -> dict:
    return {**store.stats(), "hub": hub.stats(),
            "connector": connector.status() if connector else None}


# ---------------------------------------------------------------------------
# ticketing
# ---------------------------------------------------------------------------

@app.post("/api/nodes/{node_id}/ticket")
async def create_ticket(node_id: str, principal: dict = Depends(require_role("analyst", "admin"))) -> dict:
    node = store.get_topology().node_index().get(node_id)
    if node is None:
        raise HTTPException(404, f"no such node: {node_id}")
    state = store.get_state(node_id)
    if state is None:
        raise HTTPException(409, f"node {node_id} has no active alert state")
    payload = build_ticket_payload(node, state, store.alerts_for_node(node_id, 20))
    delivery = webhook.send(payload)
    return {"status": delivery.status, "ticket_ref": delivery.ticket_ref,
            "http_status": delivery.http_status, "error": delivery.error,
            "payload": payload}


@app.get("/api/integrations/webhook")
async def webhook_log(principal: dict = Depends(authenticate)) -> dict:
    return {"configured": bool(settings.webhook_url), "dry_run": webhook.dry_run,
            "deliveries": webhook.recent(50)}


# ---------------------------------------------------------------------------
# demo / harness
# ---------------------------------------------------------------------------

@app.post("/api/simulate/incident")
async def simulate_incident(
    node_id: Optional[str] = None, scenario: Optional[str] = None,
    noise: int = Query(0, ge=0, le=500),
    seed: Optional[int] = Query(None, description="re-seed the generator for a reproducible burst"),
    principal: dict = Depends(require_role("analyst", "admin")),
) -> dict:
    """Inject a synthetic incident.

    Used by the demo and by the MTTI harness.  It goes through the *same*
    correlate-and-store path as a real Stage 1 alert, so what the harness
    measures is the real pipeline, not a shortcut.
    """
    if not settings.allow_simulation:
        raise HTTPException(403, "simulation is disabled (CN_MAP_ALLOW_SIMULATION=0)")
    if seed is not None:
        # Re-seeding makes an injection byte-for-byte reproducible, which is what
        # lets the visual-regression snapshots compare like with like instead of
        # re-photographing a different incident every run.
        import random

        _simulator.rng = random.Random(seed)
    injected_at = time.time()
    target, chosen, alerts = _simulator.incident(node_id, scenario, now=injected_at)
    if noise:
        alerts = _simulator.noise(noise, now=injected_at) + alerts

    from integration.alert_codec import to_alert

    updates = []
    for raw in alerts:
        update = store.apply_alert(to_alert(raw))
        if update:
            updates.append(update)
            await hub.publish(json.loads(update.model_dump_json()))
    return {
        "injected_at": injected_at, "node_id": target, "scenario": chosen,
        "alerts": len(alerts), "updates": len(updates),
        "server_latency_ms": (time.time() - injected_at) * 1000,
    }


# ---------------------------------------------------------------------------
# websocket
# ---------------------------------------------------------------------------

@app.websocket("/ws/stream")
async def stream(websocket: WebSocket, token: Optional[str] = None,
                 api_key: Optional[str] = None) -> None:
    principal = authenticate_ws(token, api_key)
    if principal is None:
        await websocket.close(code=4401, reason="unauthorized")
        return
    await websocket.accept()
    client = await hub.connect(websocket, principal)
    writer = asyncio.create_task(hub.writer(client))
    try:
        # Prime the client with current state so a browser opened mid-incident
        # shows the incident, rather than waiting for the next alert.
        await websocket.send_text(json.dumps({
            "type": "snapshot",
            "ts": time.time(),
            "topology_version": store.get_topology().version,
            "states": [json.loads(s.model_dump_json()) for s in store.get_states()],
        }, default=str))
        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_text(json.dumps({"type": "pong", "ts": time.time()}))
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # pragma: no cover
        log.debug("ws error: %s", exc)
    finally:
        writer.cancel()
        await hub.disconnect(client)


# ---------------------------------------------------------------------------
# static frontend (served last so /api and /ws win)
# ---------------------------------------------------------------------------

def mount_frontend() -> None:
    dist = Path(settings.static_dir)
    if not dist.exists():
        log.info("no built frontend at %s (run `npm run build` in frontend/)", dist)
        return
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(dist / "index.html")

    @app.get("/{path:path}")
    async def spa(path: str) -> FileResponse:
        candidate = dist / path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")

    log.info("serving frontend from %s", dist)


mount_frontend()


def main() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run("backend.app:app", host=settings.host, port=settings.port,
                log_level=settings.log_level.lower())


if __name__ == "__main__":  # pragma: no cover
    main()
