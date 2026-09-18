"""Backend HTTP and WebSocket surface."""
import json

import pytest


class TestAuth:
    def test_unauthenticated_requests_are_rejected(self, client):
        client.headers.pop("X-API-Key", None)
        for path in ("/api/topology", "/api/states", "/api/alerts", "/api/stats"):
            assert client.get(path).status_code == 401, path

    def test_health_is_public(self, client):
        client.headers.pop("X-API-Key", None)
        assert client.get("/api/health").status_code == 200

    def test_api_key_and_jwt_both_work(self, client):
        assert client.get("/api/states").status_code == 200
        token = client.post("/api/auth/token", json={"role": "analyst"}).json()
        client.headers.pop("X-API-Key")
        response = client.get("/api/states",
                              headers={"Authorization": f"Bearer {token['access_token']}"})
        assert response.status_code == 200

    def test_bad_api_key_cannot_mint_a_token(self, client):
        client.headers["X-API-Key"] = "wrong"
        assert client.post("/api/auth/token", json={}).status_code == 401

    def test_expired_token_is_rejected(self, client):
        import time

        import jwt as pyjwt

        from cnmap.config import settings

        stale = pyjwt.encode(
            {"sub": "x", "role": "analyst", "exp": int(time.time()) - 10},
            settings.jwt_secret, algorithm="HS256")
        client.headers.pop("X-API-Key")
        response = client.get("/api/states", headers={"Authorization": f"Bearer {stale}"})
        assert response.status_code == 401

    def test_viewer_role_cannot_mutate(self, client):
        token = client.post("/api/auth/token", json={"role": "viewer"}).json()
        client.headers.pop("X-API-Key")
        response = client.post("/api/topology/reload",
                               headers={"Authorization": f"Bearer {token['access_token']}"})
        assert response.status_code == 403


class TestTopology:
    def test_compact_is_smaller_than_full(self, client):
        compact = client.get("/api/topology?format=compact").json()
        full = client.get("/api/topology?format=full").json()
        assert compact["format"] == "compact"
        assert len(compact["nodes"]) == len(full["nodes"])
        assert len(json.dumps(compact)) < len(json.dumps(full))

    def test_compact_links_reference_nodes_by_index(self, client):
        compact = client.get("/api/topology?format=compact").json()
        count = len(compact["nodes"])
        for source, target, _kind in compact["links"]:
            assert 0 <= source < count and 0 <= target < count

    def test_etag_revalidation(self, client):
        first = client.get("/api/topology")
        etag = first.headers["etag"]
        assert client.get("/api/topology", headers={"If-None-Match": etag}).status_code == 304

    def test_positions_are_present(self, client):
        compact = client.get("/api/topology?format=compact").json()
        fields = compact["node_fields"]
        xi, yi, zi = fields.index("x"), fields.index("y"), fields.index("z")
        assert any(row[xi] or row[yi] or row[zi] for row in compact["nodes"])

    def test_stats_and_bounds(self, client):
        stats = client.get("/api/topology/stats").json()
        assert stats["nodes"] > 0 and stats["links"] > 0
        assert stats["bounds"]["radius"] > 0


class TestIncidentFlow:
    def _inject(self, client, **params):
        query = "&".join(f"{k}={v}" for k, v in params.items())
        response = client.post(f"/api/simulate/incident?{query}")
        assert response.status_code == 200
        return response.json()

    def test_injection_marks_a_node(self, client):
        result = self._inject(client, scenario="port_scan")
        states = client.get("/api/states").json()
        assert states["count"] >= 1
        assert any(s["node_id"] == result["node_id"] for s in states["states"])

    def test_node_detail_includes_state_alerts_and_neighbours(self, client):
        result = self._inject(client, scenario="exfiltration")
        detail = client.get(f"/api/nodes/{result['node_id']}").json()
        assert detail["node"]["id"] == result["node_id"]
        assert detail["state"]["severity"] in {"high", "critical"}
        assert len(detail["alerts"]) >= 1
        assert detail["telemetry"]["alert_count"] >= 1

    def test_unknown_node_is_404(self, client):
        assert client.get("/api/nodes/does-not-exist").status_code == 404

    def test_acknowledge_clears_and_is_idempotent(self, client):
        result = self._inject(client, scenario="brute_force")
        assert client.post(f"/api/nodes/{result['node_id']}/acknowledge").status_code == 200
        assert client.get("/api/states").json()["count"] == 0
        assert client.post(f"/api/nodes/{result['node_id']}/acknowledge").status_code == 404

    def test_alerts_filter_by_severity_and_node(self, client):
        result = self._inject(client, scenario="c2_beacon", noise=20)
        critical = client.get("/api/alerts?min_severity=high&limit=500").json()
        assert all(a["severity"] in {"high", "critical"} for a in critical["alerts"])
        by_node = client.get(f"/api/alerts?node_id={result['node_id']}").json()
        assert all(a["node_id"] == result["node_id"] for a in by_node["alerts"])

    def test_timeline_and_replay(self, client):
        import time

        self._inject(client, scenario="port_scan")
        timeline = client.get("/api/timeline").json()
        assert timeline["count"] >= 1
        replay = client.get(f"/api/replay?ts={time.time()}").json()
        assert replay["count"] >= 1
        old = client.get(f"/api/replay?ts={time.time() - 86400}").json()
        assert old["count"] == 0

    def test_csv_export_is_a_download(self, client):
        self._inject(client, scenario="port_scan")
        response = client.get("/api/timeline/export?format=csv")
        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        assert response.text.splitlines()[0].startswith("timestamp_iso,")

    def test_json_export(self, client):
        self._inject(client, scenario="port_scan")
        body = client.get("/api/timeline/export?format=json").json()
        assert body["count"] >= 1

    def test_ticket_creation_records_a_delivery(self, client):
        result = self._inject(client, scenario="exfiltration")
        ticket = client.post(f"/api/nodes/{result['node_id']}/ticket").json()
        assert ticket["status"] == "dry-run"
        assert ticket["payload"]["asset"]["node_id"] == result["node_id"]
        log = client.get("/api/integrations/webhook").json()
        assert any(d["node_id"] == result["node_id"] for d in log["deliveries"])

    def test_ticket_for_a_quiet_node_is_rejected(self, client):
        topology = client.get("/api/topology?format=compact").json()
        node_id = topology["nodes"][0][topology["node_fields"].index("id")]
        assert client.post(f"/api/nodes/{node_id}/ticket").status_code == 409

    def test_no_payload_data_is_exposed(self, client):
        """Stage 1 never emits payload bytes and Stage 2 must never invent a
        field that implies otherwise."""
        result = self._inject(client, scenario="exfiltration")
        detail = client.get(f"/api/nodes/{result['node_id']}").json()
        blob = json.dumps(detail).lower()
        for forbidden in ("payload", "packet_bytes", "raw_bytes", "content"):
            assert forbidden not in blob


class TestStreaming:
    def test_websocket_requires_credentials(self, client):
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/stream"):
                pass

    def test_websocket_sends_a_priming_snapshot(self, client):
        with client.websocket_connect("/ws/stream?api_key=test-key") as ws:
            frame = ws.receive_json()
            assert frame["type"] == "snapshot"
            assert "states" in frame

    def test_websocket_receives_updates(self, client):
        with client.websocket_connect("/ws/stream?api_key=test-key") as ws:
            assert ws.receive_json()["type"] == "snapshot"
            client.post("/api/simulate/incident?scenario=port_scan")
            seen = []
            for _ in range(10):
                frame = ws.receive_json()
                if frame["type"] in {"node_state", "batch"}:
                    seen.append(frame)
                    break
            assert seen, "an injected incident must reach a connected client"

    def test_snapshot_reflects_existing_state(self, client):
        result = client.post("/api/simulate/incident?scenario=exfiltration").json()
        with client.websocket_connect("/ws/stream?api_key=test-key") as ws:
            frame = ws.receive_json()
            assert any(s["node_id"] == result["node_id"] for s in frame["states"])


class TestOperational:
    def test_health_reports_subsystems(self, client):
        body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["nodes"] > 0
        assert body["auth"]["enabled"] is True
        assert "stub" in body["auth"]["mode"]

    def test_stats(self, client):
        client.post("/api/simulate/incident?scenario=port_scan")
        body = client.get("/api/stats").json()
        assert body["total_alerts"] >= 1
        assert body["nodes"] > 0

    def test_unresolved_addresses_are_reported(self, client):
        body = client.get("/api/unresolved").json()
        assert "addresses" in body and "stats" in body

    def test_topology_reload(self, client):
        before = client.get("/api/topology/stats").json()["version"]
        assert client.post("/api/topology/reload").json()["version"] > before
