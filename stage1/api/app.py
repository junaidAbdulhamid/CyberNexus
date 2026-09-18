"""Alerting service: REST + WebSocket + Prometheus metrics.

Consumes the alert stream, keeps a bounded in-memory ring of recent alerts for
querying, fans every new alert out to connected WebSocket clients, and rolls up
per-source counts so an operator sees "this host scanned 400 ports" instead of
400 separate rows.

Endpoints
    GET  /health              liveness + bus reachability + model info
    GET  /alerts              recent alerts, filterable
    GET  /alerts/summary      rollup by source address and destination port
    GET  /stats               counters and throughput
    GET  /metrics             Prometheus exposition
    POST /score               score one connection record synchronously
    WS   /ws/alerts           live alert feed

The ring buffer is deliberately in-memory: alerts are already durable in the
stream, and an operator console wants the last N, fast.  Anything that needs
history reads the stream (or a sink fed from it) directly.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, deque
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse

from cybernexus.bus import make_bus
from cybernexus.config import Settings
from cybernexus.logging_setup import setup_logging
from cybernexus.metrics import ALERTS, generate_latest
from cybernexus.records import FIELDS, N_FIELDS
from cybernexus.wire import unpack_alerts

log = logging.getLogger("api")
settings = Settings()

state: dict[str, Any] = {
    "alerts": deque(maxlen=settings.alert_buffer),
    "clients": set(),
    "by_source": Counter(),
    "by_dst_port": Counter(),
    "total": 0,
    "started": time.time(),
    "engine": None,
    "batcher": None,
    "bus": None,
}


def _engine():
    """Lazy-load the model: the alert feed must work even without one."""
    if state["engine"] is None:
        from model.infer import InferenceEngine, MicroBatcher

        engine = InferenceEngine()
        state["engine"] = engine
        state["batcher"] = MicroBatcher(engine)
    return state["engine"]


async def alert_pump() -> None:
    """Move alerts from the bus into the ring buffer and out to WebSockets."""
    import json

    bus = state["bus"]
    group = "api"
    consumer = f"api-{int(time.time())}"
    loop = asyncio.get_running_loop()
    bus.ensure_group(settings.alert_stream, group)
    while True:
        try:
            batch = await loop.run_in_executor(
                None, bus.consume, settings.alert_stream, group, consumer, 64, 500
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - bus hiccup
            log.warning("alert pump: %s", exc)
            await asyncio.sleep(1.0)
            continue
        if not batch:
            continue
        fresh = []
        for _mid, blob in batch:
            for alert in unpack_alerts(blob):
                state["alerts"].append(alert)
                state["by_source"][alert["src_ip"]] += 1
                state["by_dst_port"][alert["dst_port"]] += 1
                state["total"] += 1
                fresh.append(alert)
        ALERTS.labels("api").inc(len(fresh))
        bus.ack(settings.alert_stream, group, [mid for mid, _ in batch])
        if fresh and state["clients"]:
            payload = json.dumps(fresh[-50:])
            dead = []
            for ws in list(state["clients"]):
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                state["clients"].discard(ws)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.log_level)
    state["bus"] = make_bus(settings.bus, settings.redis_url, settings.stream_maxlen)
    task = asyncio.create_task(alert_pump())
    log.info("api up: bus=%s stream=%s", settings.bus, settings.alert_stream)
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        state["bus"].close()


app = FastAPI(
    title="CyberNexus Alerting API",
    version="1.0.0",
    description="Real-time network detection alerts",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict:
    bus_ok = bool(getattr(state["bus"], "ping", lambda: True)())
    model = None
    try:
        engine = _engine()
        model = {"name": engine.meta.model, "backend": engine.backend,
                 "threshold": engine.threshold, "trained_utc": engine.meta.created_utc}
    except Exception as exc:
        model = {"error": str(exc)}
    return {
        "status": "ok" if bus_ok else "degraded",
        "bus": {"kind": settings.bus, "reachable": bus_ok},
        "model": model,
        "uptime_s": time.time() - state["started"],
        "alerts_seen": state["total"],
    }


@app.get("/alerts")
async def list_alerts(
    limit: int = Query(50, ge=1, le=5000),
    min_score: float = Query(0.0, ge=0.0, le=1.0),
    src_ip: str | None = None,
    dst_port: int | None = None,
) -> list[dict]:
    out = []
    for alert in reversed(state["alerts"]):
        if alert["score"] < min_score:
            continue
        if src_ip and alert["src_ip"] != src_ip:
            continue
        if dst_port is not None and alert["dst_port"] != dst_port:
            continue
        out.append(alert)
        if len(out) >= limit:
            break
    return out


@app.get("/alerts/summary")
async def alert_summary(top: int = Query(10, ge=1, le=200)) -> dict:
    """Rollup view.  A scan is one event to an analyst and hundreds of flows to
    the detector; this is the cheapest bridge between the two."""
    recent = list(state["alerts"])
    return {
        "total_alerts": state["total"],
        "buffered": len(recent),
        "top_sources": [
            {"src_ip": ip, "alerts": n} for ip, n in state["by_source"].most_common(top)
        ],
        "top_dst_ports": [
            {"dst_port": port, "alerts": n} for port, n in state["by_dst_port"].most_common(top)
        ],
        "mean_score": (sum(a["score"] for a in recent) / len(recent)) if recent else 0.0,
    }


@app.get("/stats")
async def stats() -> dict:
    recent = list(state["alerts"])
    now = time.time()
    last_minute = [a for a in recent if now - a["detected_at"] <= 60]
    return {
        "alerts_total": state["total"],
        "alerts_last_60s": len(last_minute),
        "alert_rate_per_s": len(last_minute) / 60.0,
        "buffer_size": len(recent),
        "buffer_capacity": state["alerts"].maxlen,
        "websocket_clients": len(state["clients"]),
        "uptime_s": now - state["started"],
        "stream_backlog": state["bus"].length(settings.alert_stream),
    }


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest().decode(), media_type="text/plain; version=0.0.4")


@app.post("/score")
async def score_one(record: dict = Body(...)) -> dict:
    """Score a single connection record.

    Accepts either the full field dict (see ``cybernexus.records.FIELDS``) or a
    partial one; unspecified numeric fields default to 0.  Requests are
    micro-batched behind the scenes, so a burst of single-record calls costs
    roughly one inference each rather than one matrix operation each.
    """
    import numpy as np

    from cybernexus.features import extract_one

    engine = _engine()
    row = [record.get(name, "" if i < 3 else 0.0) for i, name in enumerate(FIELDS)]
    if len(row) != N_FIELDS:  # pragma: no cover - defensive
        return JSONResponse({"error": "bad record"}, status_code=400)
    features = extract_one(tuple(row))
    score = state["batcher"].submit(np.asarray(features, dtype=np.float32).reshape(1, -1))
    return {
        "flow_id": record.get("flow_id", ""),
        "score": score,
        "threshold": engine.threshold,
        "alert": bool(score >= engine.threshold),
    }


@app.websocket("/ws/alerts")
async def ws_alerts(ws: WebSocket) -> None:
    await ws.accept()
    state["clients"].add(ws)
    log.info("ws client connected (%d total)", len(state["clients"]))
    try:
        # Prime the client with what it missed, then stay open; the pump does
        # the pushing, so this side only needs to notice a disconnect.
        import json

        recent = list(state["alerts"])[-25:]
        if recent:
            await ws.send_text(json.dumps(recent))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:  # pragma: no cover
        pass
    finally:
        state["clients"].discard(ws)
        log.info("ws client disconnected (%d left)", len(state["clients"]))


def main() -> None:  # pragma: no cover - entry point
    import uvicorn

    uvicorn.run("api.app:app", host=settings.api_host, port=settings.api_port,
                log_level=settings.log_level.lower())


if __name__ == "__main__":  # pragma: no cover
    main()
