"""Live state: severity decay, acknowledgement, history replay, export."""
import time

import pytest

from cnmap.models import Alert, Severity
from cnmap.store import export_timeline_csv


@pytest.fixture
def host(example_topology):
    return next(n for n in example_topology.nodes if n.device_type.value == "workstation")


class TestApplyAlert:
    def test_alert_creates_node_state(self, store, host):
        update = store.apply_alert(Alert(src_ip=host.ip, dst_ip="203.0.113.1",
                                         dst_port=4444, score=0.97, flow_id="f1"))
        assert update is not None
        assert update.state.node_id == host.id
        assert update.state.severity is Severity.CRITICAL
        assert update.state.alert_count == 1
        assert update.state.top_ports == [4444]

    def test_unmappable_alert_returns_no_update(self, store):
        assert store.apply_alert(Alert(src_ip="198.51.100.1", dst_ip="203.0.113.1", score=0.9)) is None
        assert store.total_alerts == 1, "still counted, just not attributable"

    def test_repeat_alerts_accumulate_and_keep_the_peak(self, store, host):
        now = time.time()
        store.apply_alert(Alert(src_ip=host.ip, dst_ip="1.1.1.1", score=0.99, detected_at=now), now=now)
        store.apply_alert(Alert(src_ip=host.ip, dst_ip="1.1.1.1", score=0.40, detected_at=now), now=now)
        state = store.get_state(host.id, now=now)
        assert state.alert_count == 2
        assert state.score == pytest.approx(0.99), "a later mild alert must not mask a severe one"

    def test_sequence_numbers_increase(self, store, host):
        a = store.apply_alert(Alert(src_ip=host.ip, dst_ip="1.1.1.1", score=0.9))
        b = store.apply_alert(Alert(src_ip=host.ip, dst_ip="1.1.1.1", score=0.9))
        assert b.seq > a.seq


class TestDecay:
    def test_peak_is_held_for_the_dwell_period(self, store, host):
        """A severity that changes faster than a human can react is noise."""
        t0 = time.time()
        store.half_life_s, store.dwell_s = 10.0, 30.0
        store.apply_alert(Alert(src_ip=host.ip, dst_ip="1.1.1.1", score=0.96, detected_at=t0), now=t0)
        assert store.get_state(host.id, now=t0 + 0.2).severity is Severity.CRITICAL
        assert store.get_state(host.id, now=t0 + 29).score == pytest.approx(0.96)

    def test_score_halves_each_half_life_after_the_dwell(self, store, host):
        t0 = time.time()
        store.half_life_s, store.dwell_s = 10.0, 30.0
        store.apply_alert(Alert(src_ip=host.ip, dst_ip="1.1.1.1", score=0.96, detected_at=t0), now=t0)
        assert store.get_state(host.id, now=t0 + 40).score == pytest.approx(0.48, abs=1e-6)
        assert store.get_state(host.id, now=t0 + 50).score == pytest.approx(0.24, abs=1e-6)

    def test_node_clears_completely(self, store, host):
        t0 = time.time()
        store.half_life_s, store.dwell_s = 5.0, 5.0
        store.apply_alert(Alert(src_ip=host.ip, dst_ip="1.1.1.1", score=0.9, detected_at=t0), now=t0)
        state = store.get_state(host.id, now=t0 + 120)
        assert state.severity is Severity.NONE and state.score == 0.0

    def test_decay_sweep_emits_only_changes(self, store, host):
        """Broadcasting every slightly-lower score would be pure noise on the
        wire and would make the map flicker."""
        t0 = time.time()
        store.half_life_s, store.dwell_s = 10.0, 30.0
        store.apply_alert(Alert(src_ip=host.ip, dst_ip="1.1.1.1", score=0.96, detected_at=t0), now=t0)
        assert store.decay(now=t0 + 0.2) == [], "held at its peak during the dwell"
        assert len(store.decay(now=t0 + 45)) == 1

    def test_cleared_nodes_leave_the_active_set(self, store, host):
        t0 = time.time()
        store.half_life_s, store.dwell_s = 2.0, 2.0
        store.apply_alert(Alert(src_ip=host.ip, dst_ip="1.1.1.1", score=0.9, detected_at=t0), now=t0)
        store.decay(now=t0 + 300)
        assert store.get_states(now=t0 + 300) == []


class TestAcknowledge:
    def test_acknowledging_clears_the_highlight(self, store, host):
        store.apply_alert(Alert(src_ip=host.ip, dst_ip="1.1.1.1", score=0.99))
        state = store.acknowledge(host.id)
        assert state.acknowledged and state.severity is Severity.NONE
        assert store.get_state(host.id) is None
        assert store.get_states() == []

    def test_history_survives_acknowledgement(self, store, host):
        store.apply_alert(Alert(src_ip=host.ip, dst_ip="1.1.1.1", score=0.99, flow_id="f1"))
        store.acknowledge(host.id)
        assert len(store.timeline_range()) == 1
        assert len(store.alerts_for_node(host.id)) == 1

    def test_acknowledging_an_idle_node_is_a_no_op(self, store, host):
        assert store.acknowledge(host.id) is None


class TestHistory:
    def test_replay_reconstructs_past_state(self, store, host):
        t0 = time.time()
        store.half_life_s, store.dwell_s = 60.0, 30.0
        store.apply_alert(Alert(src_ip=host.ip, dst_ip="1.1.1.1", score=0.97, detected_at=t0), now=t0)
        at_event = store.replay_at(t0 + 1)
        assert at_event and at_event[0].node_id == host.id
        assert store.replay_at(t0 - 10) == [], "nothing had happened yet"

    def test_timeline_range_filters(self, store, host):
        t0 = time.time()
        for i in range(5):
            store.apply_alert(Alert(src_ip=host.ip, dst_ip="1.1.1.1", score=0.9,
                                    detected_at=t0 + i), now=t0 + i)
        assert len(store.timeline_range()) == 5
        assert len(store.timeline_range(start=t0 + 2)) == 3
        assert len(store.timeline_range(end=t0 + 1)) == 2

    def test_csv_export_has_a_header_and_rows(self, store, host):
        store.apply_alert(Alert(src_ip=host.ip, dst_ip="203.0.113.5", dst_port=443,
                                score=0.9, flow_id="f9"))
        lines = export_timeline_csv(store.timeline_range()).strip().splitlines()
        assert lines[0].startswith("timestamp_iso,")
        assert "f9" in lines[1] and host.id in lines[1]


class TestTopologyChange:
    def test_state_for_removed_nodes_is_dropped(self, store, host, example_topology):
        """A decommissioned host must stop glowing."""
        store.apply_alert(Alert(src_ip=host.ip, dst_ip="1.1.1.1", score=0.99))
        assert store.get_state(host.id) is not None
        trimmed = example_topology.model_copy(deep=True)
        trimmed.nodes = [n for n in trimmed.nodes if n.id != host.id]
        store.set_topology(trimmed)
        assert store.get_state(host.id) is None

    def test_version_increases(self, store, example_topology):
        before = store.get_topology().version
        assert store.set_topology(example_topology.model_copy(deep=True)).version > before


class TestOrdering:
    def test_states_are_ranked_worst_first(self, store, example_topology):
        hosts = [n for n in example_topology.nodes if n.ip][:3]
        for host, score in zip(hosts, (0.35, 0.99, 0.62)):
            store.apply_alert(Alert(src_ip=host.ip, dst_ip="1.1.1.1", score=score))
        ranks = [s.severity.rank for s in store.get_states()]
        assert ranks == sorted(ranks, reverse=True)
