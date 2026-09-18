/**
 * The backend, in the browser.
 *
 * ## Why this exists
 *
 * The real Stage 2 backend holds a WebSocket per client, a background thread
 * consuming Stage 1's Redis stream, and live in-memory node state that decays on
 * a timer. None of those survive on a serverless host: functions are stateless,
 * short-lived, and Vercel does not support WebSocket upgrades at all.
 *
 * Rather than pretend otherwise, the demo build swaps the *transport* and keeps
 * everything else. This module reimplements the parts of `cnmap/store.py` and
 * `integration/simulator.py` that the interface actually observes — severity
 * with dwell and exponential decay, the alert timeline, replay, acknowledgement,
 * and the attack scenarios — behind the exact same API surface the live client
 * talks to. The React components, the 3D scene, the command palette and the
 * keyboard layer cannot tell the difference, because they are handed the same
 * shapes.
 *
 * What is genuinely lost, and is stated plainly in the UI: there is no Stage 1
 * sensor on the other end. The alerts are synthetic, exactly as they are in the
 * `docker compose` demo — but there they travel through a real Redis stream and
 * a real correlator, and here they do not.
 *
 * Constants below mirror the Python source; the comment on each names its
 * counterpart so the two cannot drift silently.
 */

// --- cnmap/models.py :: Severity.from_score --------------------------------
export function severityFromScore(score) {
  if (score >= 0.95) return "critical";
  if (score >= 0.80) return "high";
  if (score >= 0.55) return "medium";
  if (score >= 0.30) return "low";
  return "info";
}

const RANK = { none: 0, info: 1, low: 2, medium: 3, high: 4, critical: 5 };

// --- cnmap/store.py constants ----------------------------------------------
const HALF_LIFE_S = 120;   // DEFAULT_HALF_LIFE_S
const DWELL_S = 30;        // DEFAULT_DWELL_S — hold the peak before decaying
const CLEAR_BELOW = 0.05;  // CLEAR_BELOW
const WINDOW_S = 300;      // WINDOW_S — rolling "alerts in the last N seconds"

// --- integration/simulator.py :: SCENARIOS ---------------------------------
export const SCENARIOS = {
  port_scan: { ports: [22, 23, 80, 135, 139, 443, 445, 3389, 8080, 5900], score: [0.88, 0.99], burst: [8, 20], external: false },
  c2_beacon: { ports: [443, 8443, 53, 8080], score: [0.90, 0.99], burst: [3, 6], external: true },
  exfiltration: { ports: [443, 22, 21, 4444], score: [0.93, 0.995], burst: [2, 5], external: true },
  brute_force: { ports: [22, 3389, 445], score: [0.85, 0.97], burst: [10, 25], external: false },
  lateral_movement: { ports: [445, 3389, 5985, 22], score: [0.86, 0.98], burst: [5, 12], external: false },
};
const NOISE_PORTS = [80, 443, 53, 123, 8080, 3128];

/** Deterministic PRNG so a demo session is reproducible from a seed. */
function makeRng(seed) {
  let state = seed >>> 0 || 1;
  return () => {
    state ^= state << 13; state >>>= 0;
    state ^= state >> 17;
    state ^= state << 5; state >>>= 0;
    return state / 0xffffffff;
  };
}

export class DemoEngine {
  constructor(topology, { seed = Date.now() & 0xffff, onUpdate = null } = {}) {
    this.topology = topology;
    this.rng = makeRng(seed);
    this.onUpdate = onUpdate;

    this.states = new Map();      // node_id -> NodeState
    this.alerts = [];             // newest first, bounded
    this.timeline = [];           // oldest first, bounded
    this.recentWindow = new Map();
    this.seq = 0;
    this.totalAlerts = 0;
    this.startedAt = Date.now() / 1000;

    this.hosts = topology.nodes.filter(
      (n) => n.ip && ["workstation", "server", "printer", "iot"].includes(n.device_type)
    );
    this.byId = topology.index;
    this.running = false;
  }

  // -- helpers ---------------------------------------------------------
  _pick(list) { return list[Math.floor(this.rng() * list.length)]; }
  _between(lo, hi) { return lo + this.rng() * (hi - lo); }
  _intBetween(lo, hi) { return Math.floor(this._between(lo, hi + 1)); }
  _externalIp() { return `203.0.${this._intBetween(1, 254)}.${this._intBetween(1, 254)}`; }

  _alert(node, peerIp, port, score, now) {
    return {
      flow_id: `sim-${Math.floor(this.rng() * 1e12).toString(16)}`,
      src_ip: node.ip, dst_ip: peerIp,
      src_port: this._intBetween(49152, 65535), dst_port: port,
      protocol: 6, score: Math.round(score * 1e4) / 1e4, threshold: 0.2158,
      detected_at: now, latency_s: Math.round(this._between(0.004, 0.02) * 1e4) / 1e4,
      label: score >= 0.5 ? 1 : 0,
      node_id: node.id, peer_node_id: null,
      severity: severityFromScore(score), correlation: "ip", received_at: now,
    };
  }

  // -- state -----------------------------------------------------------
  /** cnmap/store.py :: _decay_state — peak held for the dwell, then halves. */
  _decayed(state, now) {
    if (!state.score || !state.last_alert_at) return state;
    const elapsed = Math.max(now - state.last_alert_at, 0);
    if (elapsed <= DWELL_S) return state;
    const decayed = state.score * 0.5 ** ((elapsed - DWELL_S) / HALF_LIFE_S);
    const copy = { ...state };
    if (decayed < CLEAR_BELOW) {
      copy.score = 0; copy.severity = "none";
    } else {
      copy.score = decayed; copy.severity = severityFromScore(decayed);
    }
    return copy;
  }

  applyAlert(alert) {
    const now = alert.detected_at;
    this.totalAlerts += 1;
    this.alerts.unshift(alert);
    if (this.alerts.length > 2000) this.alerts.length = 2000;

    let state = this.states.get(alert.node_id);
    if (!state) {
      state = {
        node_id: alert.node_id, severity: "none", score: 0, alert_count: 0,
        alert_count_window: 0, first_alert_at: null, last_alert_at: null,
        last_alert_id: "", top_peers: [], top_ports: [], acknowledged: false,
        updated_at: now,
      };
    } else {
      state = this._decayed(state, now);
    }

    state.score = Math.max(state.score, alert.score);
    state.severity = severityFromScore(state.score);
    state.alert_count += 1;
    state.first_alert_at = state.first_alert_at ?? alert.detected_at;
    state.last_alert_at = alert.detected_at;
    state.last_alert_id = alert.flow_id;
    state.updated_at = now;
    state.acknowledged = false;
    state.top_ports = [alert.dst_port, ...state.top_ports.filter((p) => p !== alert.dst_port)].slice(0, 5);
    state.top_peers = [alert.dst_ip, ...state.top_peers.filter((p) => p !== alert.dst_ip)].slice(0, 5);

    const window = this.recentWindow.get(alert.node_id) || [];
    window.push(now);
    while (window.length && window[0] < now - WINDOW_S) window.shift();
    this.recentWindow.set(alert.node_id, window);
    state.alert_count_window = window.length;

    this.states.set(alert.node_id, state);
    this.timeline.push({
      ts: alert.detected_at, node_id: alert.node_id, severity: alert.severity,
      score: alert.score, flow_id: alert.flow_id, src_ip: alert.src_ip,
      dst_ip: alert.dst_ip, dst_port: alert.dst_port,
    });
    if (this.timeline.length > 20000) this.timeline.shift();

    this.seq += 1;
    this.onUpdate?.({ type: "node_state", state: { ...state }, alert, seq: this.seq });
  }

  /** Sweep every node and emit only genuine severity changes. */
  decay() {
    const now = Date.now() / 1000;
    for (const [nodeId, state] of [...this.states]) {
      const next = this._decayed(state, now);
      if (next.severity === state.severity) continue;
      next.updated_at = now;
      this.seq += 1;
      if (next.severity === "none") {
        this.states.delete(nodeId);
        this.recentWindow.delete(nodeId);
      } else {
        this.states.set(nodeId, next);
      }
      this.onUpdate?.({ type: "node_state", state: { ...next }, seq: this.seq });
    }
  }

  getStates(now = Date.now() / 1000) {
    const out = [];
    for (const state of this.states.values()) {
      const next = this._decayed(state, now);
      if (next.severity !== "none") out.push(next);
    }
    return out.sort(
      (a, b) => RANK[b.severity] - RANK[a.severity] || (b.last_alert_at || 0) - (a.last_alert_at || 0)
    );
  }

  acknowledge(nodeId) {
    const state = this.states.get(nodeId);
    if (!state) return null;
    this.states.delete(nodeId);
    this.recentWindow.delete(nodeId);
    const cleared = { ...state, acknowledged: true, severity: "none", score: 0,
                      updated_at: Date.now() / 1000 };
    this.seq += 1;
    this.onUpdate?.({ type: "node_state", state: cleared, seq: this.seq });
    return cleared;
  }

  /** cnmap/store.py :: replay_at — rebuild state from the event log. */
  replayAt(ts) {
    const states = new Map();
    for (const event of this.timeline) {
      if (event.ts > ts) break;
      let state = states.get(event.node_id);
      if (!state) {
        state = { node_id: event.node_id, severity: "none", score: 0, alert_count: 0,
                  alert_count_window: 0, first_alert_at: event.ts, last_alert_at: null,
                  last_alert_id: "", top_peers: [], top_ports: [], acknowledged: false,
                  updated_at: event.ts };
        states.set(event.node_id, state);
      }
      const gap = Math.max(event.ts - (state.last_alert_at ?? event.ts), 0);
      const faded = Math.max(gap - DWELL_S, 0);
      state.score = Math.max(state.score * 0.5 ** (faded / HALF_LIFE_S), event.score);
      state.alert_count += 1;
      state.last_alert_at = event.ts;
      state.last_alert_id = event.flow_id;
      state.severity = severityFromScore(state.score);
    }
    const out = [];
    for (const state of states.values()) {
      const next = this._decayed(state, ts);
      if (next.severity !== "none") out.push(next);
    }
    return out.sort(
      (a, b) => RANK[b.severity] - RANK[a.severity] || (b.last_alert_at || 0) - (a.last_alert_at || 0)
    );
  }

  // -- generation ------------------------------------------------------
  noise(count = 1) {
    const now = Date.now() / 1000;
    for (let i = 0; i < count; i += 1) {
      const node = this._pick(this.hosts);
      const peer = this.rng() < 0.6 ? this._externalIp() : this._pick(this.hosts).ip;
      this.applyAlert(this._alert(node, peer, this._pick(NOISE_PORTS), this._between(0.22, 0.45), now));
    }
  }

  incident({ nodeId = null, scenario = null } = {}) {
    const name = scenario && SCENARIOS[scenario] ? scenario : this._pick(Object.keys(SCENARIOS));
    const spec = SCENARIOS[name];
    const node = nodeId
      ? this.hosts.find((h) => h.id === nodeId) || this._pick(this.hosts)
      : this._pick(this.hosts);
    const now = Date.now() / 1000;
    const burst = this._intBetween(spec.burst[0], spec.burst[1]);
    for (let i = 0; i < burst; i += 1) {
      const peer = spec.external ? this._externalIp() : this._pick(this.hosts).ip;
      this.applyAlert(this._alert(
        node, peer, this._pick(spec.ports), this._between(spec.score[0], spec.score[1]), now
      ));
    }
    return { node_id: node.id, scenario: name, alerts: burst };
  }

  /** Background noise plus a periodic incident, matching the compose demo. */
  start({ noisePerSecond = 1.5, incidentEverySeconds = 20, seedIncidents = 3 } = {}) {
    if (this.running) return;
    this.running = true;

    for (let i = 0; i < seedIncidents; i += 1) this.incident();
    this.noise(18);

    this._noiseTimer = setInterval(() => this.noise(1), Math.max(1000 / noisePerSecond, 120));
    this._incidentTimer = setInterval(() => this.incident(), incidentEverySeconds * 1000);
    this._decayTimer = setInterval(() => this.decay(), 2000);
  }

  stop() {
    this.running = false;
    clearInterval(this._noiseTimer);
    clearInterval(this._incidentTimer);
    clearInterval(this._decayTimer);
  }

  stats() {
    const active = this.getStates();
    const bySeverity = {};
    for (const state of active) bySeverity[state.severity] = (bySeverity[state.severity] || 0) + 1;
    return {
      topology_version: this.topology.version,
      nodes: this.topology.nodes.length,
      links: this.topology.links.length,
      alerting_nodes: active.length,
      by_severity: bySeverity,
      total_alerts: this.totalAlerts,
      timeline_events: this.timeline.length,
      uptime_s: Date.now() / 1000 - this.startedAt,
      half_life_s: HALF_LIFE_S,
      dwell_s: DWELL_S,
      mode: "demo",
    };
  }
}
