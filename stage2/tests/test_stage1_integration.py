"""Stage 1 -> Stage 2 integration: codec compatibility and the connector.

The alert wire format is declared twice - once in Stage 1's ``cybernexus.wire``
and once in Stage 2's ``integration/alert_codec.py`` - so that Stage 2 can be
deployed without a copy of Stage 1 on the PYTHONPATH. Duplication like that is
only safe if something checks it, which is what the first test class does
whenever the Stage 1 tree is importable.
"""
import sys
import time
from pathlib import Path

import pytest

from cnmap.models import Alert
from cnmap.store import MapStore
from integration.alert_codec import decode_entry, pack_alert_batch, to_alert, unpack_alert_batch
from integration.connector import Stage1Connector
from integration.simulator import SCENARIOS, AlertSimulator
from integration.webhook import WebhookSender, build_ticket_payload

STAGE1_DIR = Path(__file__).resolve().parent.parent.parent / "stage1"


def stage1_available() -> bool:
    if not (STAGE1_DIR / "cybernexus" / "wire.py").exists():
        return False
    if str(STAGE1_DIR) not in sys.path:
        sys.path.insert(0, str(STAGE1_DIR))
    try:
        import cybernexus.wire  # noqa: F401  (import is the availability check)

        return True
    except Exception:
        return False


class TestStage1Codec:
    @pytest.mark.skipif(not stage1_available(), reason="Stage 1 tree not present")
    def test_stage2_decodes_what_stage1_encodes(self):
        """The two independent declarations of the format must agree."""
        from cybernexus.wire import pack_alerts as stage1_pack

        alerts = [{
            "flow_id": "f-abc", "src_ip": "10.20.10.100", "dst_ip": "203.0.113.9",
            "src_port": 51000, "dst_port": 4444, "protocol": 6, "score": 0.97,
            "threshold": 0.2158, "detected_at": 1700000000.5, "latency_s": 0.0071,
            "label": 1.0,
        }]
        decoded = decode_entry(stage1_pack(alerts))
        assert len(decoded) == 1
        alert = decoded[0]
        assert alert.flow_id == "f-abc"
        assert alert.src_ip == "10.20.10.100"
        assert alert.dst_port == 4444
        assert alert.score == pytest.approx(0.97)
        assert alert.detected_at == pytest.approx(1700000000.5)

    @pytest.mark.skipif(not stage1_available(), reason="Stage 1 tree not present")
    def test_stage1_field_name_matches(self):
        from cybernexus.bus import FIELD as stage1_field

        from integration.alert_codec import STREAM_FIELD

        assert STREAM_FIELD == stage1_field

    def test_roundtrip_through_stage2s_own_encoder(self):
        raw = [{"flow_id": "x", "src_ip": "10.0.0.1", "dst_ip": "10.0.0.2", "score": 0.5}]
        assert unpack_alert_batch(pack_alert_batch(raw))[0]["flow_id"] == "x"

    def test_unknown_fields_are_tolerated(self):
        """Stage 1 adding a field must not take the map offline."""
        alert = to_alert({"flow_id": "x", "src_ip": "10.0.0.1", "score": 0.9,
                          "a_new_field_from_a_future_stage_1": 123})
        assert alert.flow_id == "x" and alert.score == pytest.approx(0.9)

    def test_missing_fields_get_safe_defaults(self):
        alert = to_alert({})
        assert alert.score == 0.0 and alert.protocol == 6 and alert.src_ip == ""

    def test_single_dict_entry_is_accepted(self):
        import msgpack

        blob = msgpack.packb({"flow_id": "solo", "score": 0.4}, use_bin_type=True)
        assert decode_entry(blob)[0].flow_id == "solo"


class FakeRedis:
    """Just enough Redis to drive the connector without a server."""

    def __init__(self, entries):
        self.entries = list(entries)
        self.acked = []
        self.groups = []

    def xgroup_create(self, stream, group, id="$", mkstream=False):
        self.groups.append((stream, group))

    def xreadgroup(self, group, consumer, streams, count=10, block=0):
        if not self.entries:
            return []
        batch, self.entries = self.entries[:count], self.entries[count:]
        stream = list(streams)[0]
        return [(stream, batch)]

    def xack(self, stream, group, *ids):
        self.acked.extend(ids)


class TestConnector:
    @pytest.fixture
    def topology(self, example_topology):
        return example_topology

    def test_alerts_become_node_state(self, topology):
        host = next(n for n in topology.nodes if n.device_type.value == "server")
        store = MapStore(topology.model_copy(deep=True))
        blob = pack_alert_batch([{
            "flow_id": "f1", "src_ip": host.ip, "dst_ip": "203.0.113.9",
            "dst_port": 4444, "protocol": 6, "score": 0.98,
            "detected_at": time.time(), "label": 1.0,
        }])
        updates = []
        redis = FakeRedis([(b"1-0", {b"b": blob})])
        connector = Stage1Connector(store, redis, on_update=updates.append)
        connector.ensure_group()
        assert connector.poll_once() == 1
        assert updates[0].state.node_id == host.id
        assert redis.acked == [b"1-0"], "messages must be acknowledged"

    def test_batched_entries_are_all_processed(self, topology):
        hosts = [n for n in topology.nodes if n.ip][:5]
        store = MapStore(topology.model_copy(deep=True))
        blob = pack_alert_batch([
            {"flow_id": f"f{i}", "src_ip": h.ip, "dst_ip": "203.0.113.9",
             "dst_port": 443, "score": 0.9, "detected_at": time.time()}
            for i, h in enumerate(hosts)
        ])
        connector = Stage1Connector(store, FakeRedis([(b"1-0", {b"b": blob})]))
        connector.ensure_group()
        assert connector.poll_once() == 5
        assert len(store.get_states()) == 5

    def test_unmappable_alerts_are_counted_but_emit_nothing(self, topology):
        store = MapStore(topology.model_copy(deep=True))
        blob = pack_alert_batch([{"flow_id": "f", "src_ip": "198.51.100.1",
                                  "dst_ip": "203.0.113.1", "score": 0.99}])
        updates = []
        connector = Stage1Connector(store, FakeRedis([(b"1-0", {b"b": blob})]),
                                    on_update=updates.append)
        connector.ensure_group()
        assert connector.poll_once() == 1
        assert updates == []
        assert connector.status()["alerts_seen"] == 1

    def test_empty_read_is_harmless(self, topology):
        connector = Stage1Connector(MapStore(topology.model_copy(deep=True)), FakeRedis([]))
        connector.ensure_group()
        assert connector.poll_once() == 0


class TestSimulator:
    def test_incident_targets_the_requested_host(self, example_topology):
        simulator = AlertSimulator(example_topology, seed=1)
        host = next(n for n in example_topology.nodes if n.device_type.value == "workstation")
        node_id, scenario, alerts = simulator.incident(host.id, "port_scan")
        assert node_id == host.id and scenario == "port_scan"
        assert all(a["src_ip"] == host.ip for a in alerts)
        assert all(a["score"] >= 0.8 for a in alerts)

    def test_scenarios_use_their_own_ports(self, example_topology):
        simulator = AlertSimulator(example_topology, seed=2)
        _, _, alerts = simulator.incident(scenario="brute_force")
        assert set(a["dst_port"] for a in alerts) <= set(SCENARIOS["brute_force"]["ports"])

    def test_noise_is_low_severity(self, example_topology):
        simulator = AlertSimulator(example_topology, seed=3)
        assert all(0.2 < a["score"] < 0.5 for a in simulator.noise(50))

    def test_deterministic_for_a_seed(self, example_topology):
        a = AlertSimulator(example_topology, seed=7).incident(scenario="c2_beacon")
        b = AlertSimulator(example_topology, seed=7).incident(scenario="c2_beacon")
        assert a[0] == b[0] and len(a[2]) == len(b[2])

    def test_unknown_host_is_rejected(self, example_topology):
        with pytest.raises(ValueError):
            AlertSimulator(example_topology, seed=1).incident("no-such-node")


class TestWebhook:
    def test_dry_run_records_without_sending(self, example_topology):
        from cnmap.models import NodeState, Severity

        node = example_topology.nodes[0]
        state = NodeState(node_id=node.id, severity=Severity.CRITICAL, score=0.98, alert_count=4)
        sender = WebhookSender(dry_run=True)
        delivery = sender.send(build_ticket_payload(node, state, []))
        assert delivery.status == "dry-run"
        assert sender.recent()[0]["node_id"] == node.id

    def test_payload_carries_asset_and_evidence_but_no_payload_bytes(self, example_topology):
        import json

        from cnmap.models import NodeState, Severity

        node = example_topology.nodes[0]
        state = NodeState(node_id=node.id, severity=Severity.HIGH, score=0.9, alert_count=2)
        alerts = [Alert(flow_id="f1", src_ip=node.ip or "10.0.0.1", dst_ip="203.0.113.1",
                        dst_port=445, score=0.91)]
        payload = build_ticket_payload(node, state, alerts)
        assert payload["asset"]["node_id"] == node.id
        assert payload["evidence"][0]["flow_id"] == "f1"
        assert "payload" not in json.dumps(payload["evidence"]).lower()

    def test_failed_delivery_is_recorded_not_raised(self):
        sender = WebhookSender(url="http://127.0.0.1:9/never", max_attempts=1, timeout=0.2)
        delivery = sender.send({"title": "t", "asset": {"node_id": "n1"}})
        assert delivery.status == "failed"
        assert sender.recent()[0]["status"] == "failed"
