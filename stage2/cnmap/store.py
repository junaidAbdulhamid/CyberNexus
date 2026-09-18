"""Live map state: topology, per-node alert state, and history.

Three things live here and they have very different lifetimes:

* **topology** — changes on a discovery run, i.e. minutes to hours.  Snapshot,
  versioned, served over REST and cached by the client.
* **node state** — changes on every alert and decays continuously.  Streamed.
* **timeline** — an append-only ring of events, which is what the history
  scrubber replays and what the CSV/JSON export dumps.

**Severity decays.**  A node that alerted two minutes ago is not as interesting
as one alerting right now, and a map where everything that ever alerted stays red
is a map that is red.  Decay is exponential with a configurable half-life, and
crucially the *decayed* state is what gets rendered, so the map cools down on its
own without anyone clicking "acknowledge".

Two backends behind one interface: an in-process dict (default, no dependencies)
and Redis (shared state across backend replicas).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Iterable, Optional

from .correlate import Correlator
from .models import (
    Alert, NodeState, NodeStateUpdate, Severity, TimelineEvent, Topology,
)

log = logging.getLogger(__name__)

#: A node's score halves every this many seconds with no new alerts.
DEFAULT_HALF_LIFE_S = 120.0
#: Decay does not begin until a node has held its peak for this long.
#:
#: Without a dwell period, a node scoring 0.96 crosses the 0.95 "critical"
#: boundary within a fraction of a second and the map visibly downgrades an
#: incident while the operator is still turning to look at it. Severity is a
#: signal to a human, and a signal that changes faster than a human can react is
#: noise. The peak is held, then it decays.
DEFAULT_DWELL_S = 30.0
#: Below this, a node is considered clear and rendered as normal.
CLEAR_BELOW = 0.05
#: Rolling window for "alerts in the last N seconds".
WINDOW_S = 300.0


class MapStore:
    """Thread-safe live state for one map."""

    def __init__(
        self,
        topology: Optional[Topology] = None,
        half_life_s: float = DEFAULT_HALF_LIFE_S,
        dwell_s: float = DEFAULT_DWELL_S,
        timeline_size: int = 20_000,
        redis_client=None,
        namespace: str = "cnmap",
    ):
        self._lock = threading.RLock()
        self.half_life_s = half_life_s
        self.dwell_s = dwell_s
        self.namespace = namespace
        self.redis = redis_client
        self.topology = topology or Topology()
        self.correlator = Correlator.from_topology(self.topology)
        self.states: dict[str, NodeState] = {}
        # (timestamp, node_id, score) triples backing the rolling window count.
        self._recent: dict[str, deque[float]] = {}
        self.timeline: deque[TimelineEvent] = deque(maxlen=timeline_size)
        self.alerts: deque[Alert] = deque(maxlen=5_000)
        self.seq = 0
        self.total_alerts = 0
        self.started_at = time.time()

    # -- topology --------------------------------------------------------
    def set_topology(self, topology: Topology) -> Topology:
        with self._lock:
            topology.version = max(topology.version, self.topology.version + 1)
            topology.generated_at = time.time()
            self.topology = topology
            self.correlator.reindex(topology)
            valid = {n.id for n in topology.nodes}
            # Drop state for nodes that no longer exist, or the map keeps
            # rendering alerts for a decommissioned host.
            for node_id in list(self.states):
                if node_id not in valid:
                    self.states.pop(node_id, None)
                    self._recent.pop(node_id, None)
            self._persist_topology()
            return topology

    def get_topology(self) -> Topology:
        with self._lock:
            return self.topology

    # -- alerts ----------------------------------------------------------
    def apply_alert(self, alert: Alert, now: Optional[float] = None) -> Optional[NodeStateUpdate]:
        """Correlate, fold into node state, and return the update to broadcast."""
        now = now or time.time()
        with self._lock:
            self.correlator.apply(alert)
            alert.received_at = now
            self.total_alerts += 1
            self.alerts.append(alert)
            if not alert.node_id:
                return None

            state = self.states.get(alert.node_id)
            if state is None:
                state = NodeState(node_id=alert.node_id)
                self.states[alert.node_id] = state

            # Decay what is already there before folding in the new alert, so
            # two alerts a minute apart do not add up as if they were
            # simultaneous.
            self._decay_state(state, now)

            state.score = max(state.score, alert.score)
            state.severity = Severity.from_score(state.score)
            state.alert_count += 1
            state.first_alert_at = state.first_alert_at or alert.detected_at
            state.last_alert_at = alert.detected_at
            state.last_alert_id = alert.flow_id
            state.updated_at = now
            state.acknowledged = False

            recent = self._recent.setdefault(alert.node_id, deque())
            recent.append(now)
            cutoff = now - WINDOW_S
            while recent and recent[0] < cutoff:
                recent.popleft()
            state.alert_count_window = len(recent)

            peer = alert.peer_node_id or alert.dst_ip
            if peer:
                state.top_peers = ([peer] + [p for p in state.top_peers if p != peer])[:5]
            if alert.dst_port:
                state.top_ports = (
                    [alert.dst_port] + [p for p in state.top_ports if p != alert.dst_port]
                )[:5]

            self.timeline.append(TimelineEvent(
                ts=alert.detected_at, node_id=alert.node_id, severity=alert.severity,
                score=alert.score, flow_id=alert.flow_id, src_ip=alert.src_ip,
                dst_ip=alert.dst_ip, dst_port=alert.dst_port,
            ))
            self.seq += 1
            self._persist_state(state)
            return NodeStateUpdate(state=state.model_copy(), alert=alert, seq=self.seq)

    # -- decay -----------------------------------------------------------
    def _decay_state(self, state: NodeState, now: float) -> bool:
        """Apply exponential decay in place.  True when the severity changed."""
        if state.score <= 0 or not state.last_alert_at:
            return False
        elapsed = max(now - state.last_alert_at, 0.0)
        if elapsed <= self.dwell_s:
            return False
        decayed = state.score * (0.5 ** ((elapsed - self.dwell_s) / self.half_life_s))
        before = state.severity
        if decayed < CLEAR_BELOW:
            state.score = 0.0
            state.severity = Severity.NONE
        else:
            state.score = decayed
            state.severity = Severity.from_score(decayed)
        return state.severity != before

    def decay(self, now: Optional[float] = None) -> list[NodeStateUpdate]:
        """Sweep every node; returns updates for those whose severity changed.

        Called on a timer by the backend.  Only *changes* are broadcast — a
        continuous stream of slightly-lower scores would be pure noise on the
        wire and would make the UI flicker.
        """
        now = now or time.time()
        updates: list[NodeStateUpdate] = []
        with self._lock:
            for node_id, state in list(self.states.items()):
                if self._decay_state(state, now):
                    state.updated_at = now
                    self.seq += 1
                    updates.append(NodeStateUpdate(state=state.model_copy(), seq=self.seq))
                    if state.severity == Severity.NONE:
                        self.states.pop(node_id, None)
                        self._recent.pop(node_id, None)
                    else:
                        self._persist_state(state)
        return updates

    # -- reads -----------------------------------------------------------
    def get_states(self, now: Optional[float] = None) -> list[NodeState]:
        """Current state of every alerting node, decayed to ``now``."""
        now = now or time.time()
        with self._lock:
            out = []
            for state in self.states.values():
                copy = state.model_copy()
                self._decay_state(copy, now)
                if copy.severity != Severity.NONE:
                    out.append(copy)
            return sorted(out, key=lambda s: (-s.severity.rank, -(s.last_alert_at or 0)))

    def get_state(self, node_id: str, now: Optional[float] = None) -> Optional[NodeState]:
        with self._lock:
            state = self.states.get(node_id)
            if not state:
                return None
            copy = state.model_copy()
            self._decay_state(copy, now or time.time())
            return copy

    def acknowledge(self, node_id: str) -> Optional[NodeState]:
        """Clear a node's live alert state.

        Acknowledging removes the node from the active set rather than just
        flagging it: an operator who has dealt with a host expects it to stop
        glowing, and a map that keeps rendering acknowledged incidents
        accumulates them until nothing stands out. The history is untouched -
        the timeline and the alert log still have every event, so acknowledging
        loses no evidence.
        """
        with self._lock:
            state = self.states.pop(node_id, None)
            self._recent.pop(node_id, None)
            if state is None:
                return None
            state.acknowledged = True
            state.severity = Severity.NONE
            state.score = 0.0
            state.updated_at = time.time()
            if self.redis:
                try:
                    self.redis.hdel(f"{self.namespace}:states", node_id)
                except Exception:  # pragma: no cover
                    pass
            return state.model_copy()

    def alerts_for_node(self, node_id: str, limit: int = 50) -> list[Alert]:
        with self._lock:
            return [a for a in reversed(self.alerts) if a.node_id == node_id][:limit]

    def recent_alerts(self, limit: int = 100, min_severity: Severity = Severity.INFO) -> list[Alert]:
        with self._lock:
            out = []
            for alert in reversed(self.alerts):
                if alert.severity.rank >= min_severity.rank:
                    out.append(alert)
                    if len(out) >= limit:
                        break
            return out

    def timeline_range(self, start: Optional[float] = None, end: Optional[float] = None,
                       limit: int = 10_000) -> list[TimelineEvent]:
        with self._lock:
            events = list(self.timeline)
        if start is not None:
            events = [e for e in events if e.ts >= start]
        if end is not None:
            events = [e for e in events if e.ts <= end]
        return events[-limit:]

    def replay_at(self, ts: float) -> list[NodeState]:
        """Reconstruct node state as it was at ``ts`` — the history scrubber.

        Replays from the timeline rather than storing snapshots: the event log
        is small, and a snapshot per tick would be both larger and lossy between
        ticks.
        """
        states: dict[str, NodeState] = {}
        for event in self.timeline_range(end=ts):
            state = states.get(event.node_id)
            if state is None:
                state = NodeState(node_id=event.node_id, first_alert_at=event.ts)
                states[event.node_id] = state
            gap = max(event.ts - (state.last_alert_at or event.ts), 0.0)
            faded = max(gap - self.dwell_s, 0.0)
            state.score = max(state.score * (0.5 ** (faded / self.half_life_s)), event.score)
            state.alert_count += 1
            state.last_alert_at = event.ts
            state.last_alert_id = event.flow_id
            state.severity = Severity.from_score(state.score)
        out = []
        for state in states.values():
            self._decay_state(state, ts)
            if state.severity != Severity.NONE:
                out.append(state)
        return sorted(out, key=lambda s: (-s.severity.rank, -(s.last_alert_at or 0)))

    def stats(self) -> dict:
        with self._lock:
            active = self.get_states()
            by_severity: dict[str, int] = {}
            for state in active:
                by_severity[state.severity.value] = by_severity.get(state.severity.value, 0) + 1
            return {
                "topology_version": self.topology.version,
                "nodes": len(self.topology.nodes),
                "links": len(self.topology.links),
                "alerting_nodes": len(active),
                "by_severity": by_severity,
                "total_alerts": self.total_alerts,
                "timeline_events": len(self.timeline),
                "uptime_s": time.time() - self.started_at,
                "correlation": self.correlator.stats(),
                "half_life_s": self.half_life_s,
                "dwell_s": self.dwell_s,
            }

    # -- persistence -----------------------------------------------------
    def _persist_topology(self) -> None:
        if not self.redis:
            return
        try:
            self.redis.set(f"{self.namespace}:topology", self.topology.model_dump_json())
        except Exception as exc:  # pragma: no cover - redis optional
            log.warning("could not persist topology: %s", exc)

    def _persist_state(self, state: NodeState) -> None:
        if not self.redis:
            return
        try:
            self.redis.hset(f"{self.namespace}:states", state.node_id, state.model_dump_json())
        except Exception as exc:  # pragma: no cover
            log.debug("could not persist node state: %s", exc)

    def load_from_redis(self) -> bool:
        """Restore topology and states written by another replica."""
        if not self.redis:
            return False
        try:
            raw = self.redis.get(f"{self.namespace}:topology")
            if raw:
                self.set_topology(Topology.model_validate_json(raw))
            states = self.redis.hgetall(f"{self.namespace}:states") or {}
            with self._lock:
                for raw_state in states.values():
                    state = NodeState.model_validate_json(raw_state)
                    self.states[state.node_id] = state
            return True
        except Exception as exc:
            log.warning("could not load state from redis: %s", exc)
            return False


def export_timeline_csv(events: Iterable[TimelineEvent]) -> str:
    """CSV export for the "give me this incident as a file" button."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp_iso", "timestamp_epoch", "node_id", "severity",
                     "score", "src_ip", "dst_ip", "dst_port", "flow_id"])
    for e in events:
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(e.ts)) + f".{int((e.ts % 1) * 1000):03d}Z"
        writer.writerow([iso, f"{e.ts:.3f}", e.node_id, e.severity.value,
                         f"{e.score:.4f}", e.src_ip, e.dst_ip, e.dst_port, e.flow_id])
    return buf.getvalue()
