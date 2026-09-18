"""API tests.

The app is imported with ``CN_BUS=memory`` so it talks to the in-process bus;
alerts are injected by publishing to that bus exactly as the scorer would.
"""
import importlib
import json
import os
import time

import pytest

from cybernexus.wire import pack_alerts

fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture
def api(monkeypatch, model_dir):
    monkeypatch.setenv("CN_BUS", "memory")
    monkeypatch.setenv("CN_ALERT_STREAM", f"t:api:alerts:{os.urandom(3).hex()}")
    monkeypatch.setenv("CN_MODEL_DIR", str(model_dir))
    import api.app as app_module

    importlib.reload(app_module)
    return app_module


def make_alert(**kw):
    alert = {
        "flow_id": "f1", "src_ip": "10.4.1.2", "dst_ip": "203.0.113.9",
        "src_port": 51000, "dst_port": 22, "protocol": 6, "score": 0.91,
        "threshold": 0.3, "detected_at": time.time(), "latency_s": 0.004, "label": 1.0,
    }
    alert.update(kw)
    return alert


def push(app_module, alerts):
    """Publish alerts and wait for the pump to pick them up."""
    from cybernexus.bus import make_bus

    bus = make_bus("memory")
    bus.publish(app_module.settings.alert_stream, [pack_alerts(alerts)])
    deadline = time.time() + 5
    while time.time() < deadline:
        if len(app_module.state["alerts"]) >= len(alerts):
            return
        time.sleep(0.05)
    raise AssertionError("alert pump did not deliver")


def test_health(api):
    with fastapi_testclient.TestClient(api.app) as client:
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["bus"]["kind"] == "memory"
        assert body["model"]["threshold"] > 0


def test_alerts_endpoint_returns_newest_first(api):
    with fastapi_testclient.TestClient(api.app) as client:
        push(api, [make_alert(flow_id=f"f{i}", score=0.5 + i / 100) for i in range(5)])
        body = client.get("/alerts?limit=3").json()
        assert [a["flow_id"] for a in body] == ["f4", "f3", "f2"]


def test_alerts_filters(api):
    with fastapi_testclient.TestClient(api.app) as client:
        push(api, [
            make_alert(flow_id="a", src_ip="10.0.0.1", dst_port=22, score=0.99),
            make_alert(flow_id="b", src_ip="10.0.0.2", dst_port=443, score=0.40),
        ])
        assert [a["flow_id"] for a in client.get("/alerts?src_ip=10.0.0.1").json()] == ["a"]
        assert [a["flow_id"] for a in client.get("/alerts?dst_port=443").json()] == ["b"]
        assert [a["flow_id"] for a in client.get("/alerts?min_score=0.9").json()] == ["a"]


def test_summary_rolls_up_by_source(api):
    with fastapi_testclient.TestClient(api.app) as client:
        push(api, [make_alert(flow_id=f"s{i}", src_ip="10.9.9.9", dst_port=445)
                   for i in range(20)] + [make_alert(flow_id="other", src_ip="10.1.1.1")])
        body = client.get("/alerts/summary").json()
        assert body["top_sources"][0] == {"src_ip": "10.9.9.9", "alerts": 20}
        assert body["top_dst_ports"][0]["dst_port"] == 445
        assert body["total_alerts"] == 21


def test_stats(api):
    with fastapi_testclient.TestClient(api.app) as client:
        push(api, [make_alert()])
        body = client.get("/stats").json()
        assert body["alerts_total"] == 1
        assert body["alerts_last_60s"] == 1
        assert body["buffer_capacity"] >= 1


def test_metrics_is_prometheus_text(api):
    with fastapi_testclient.TestClient(api.app) as client:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "cn_" in resp.text


def test_score_endpoint(api):
    with fastapi_testclient.TestClient(api.app) as client:
        body = client.post("/score", json={
            "flow_id": "probe-1", "src_ip": "10.0.0.5", "dst_ip": "10.0.0.6",
            "src_port": 51000, "dst_port": 22, "protocol": 6, "duration": 0.9,
            "orig_bytes": 900, "resp_bytes": 900, "orig_pkts": 10, "resp_pkts": 10,
            "syn_count": 1, "iat_mean": 0.05, "iat_std": 0.005,
            "pkt_size_mean": 90, "pkt_size_std": 5,
        }).json()
        assert 0.0 <= body["score"] <= 1.0
        assert body["alert"] == (body["score"] >= body["threshold"])
        assert body["flow_id"] == "probe-1"


def test_score_endpoint_accepts_partial_record(api):
    with fastapi_testclient.TestClient(api.app) as client:
        body = client.post("/score", json={"dst_port": 443}).json()
        assert 0.0 <= body["score"] <= 1.0


def test_websocket_receives_backlog_and_live_alerts(api):
    with fastapi_testclient.TestClient(api.app) as client:
        push(api, [make_alert(flow_id="history")])
        with client.websocket_connect("/ws/alerts") as ws:
            first = json.loads(ws.receive_text())
            assert first[-1]["flow_id"] == "history"
            push(api, [make_alert(flow_id="live")])
            live = json.loads(ws.receive_text())
            assert live[-1]["flow_id"] == "live"
