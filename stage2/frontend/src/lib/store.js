/**
 * Live alert state, kept out of React's render path.
 *
 * During an incident a single node can produce tens of updates a second, and a
 * `setState` per update would spend the whole frame budget in React reconciling
 * a list nobody is reading. So updates land in a plain Map synchronously (the
 * 3D scene reads it directly and redraws itself), and React is notified at a
 * fixed 10 Hz - fast enough that the panels feel live, slow enough that the
 * render loop is never the bottleneck.
 *
 * The 100 ms flush interval is also well inside the brief's sub-second budget
 * for "alert arrives -> node visibly changes": the scene itself is updated on
 * the spot, so the 3D marking is immediate and only the side panels wait.
 */
export class AlertStore {
  constructor({ flushMs = 100, maxFeed = 500 } = {}) {
    this.states = new Map();
    this.feed = [];
    this.maxFeed = maxFeed;
    this.flushMs = flushMs;
    this.listeners = new Set();
    this.version = 0;
    this.lastUpdateAt = 0;
    this.pendingNotify = false;
    this.onSceneUpdate = null;      // synchronous hook for the 3D scene
    this.announcements = [];
    this.counters = { updates: 0, alerts: 0, maxSeverityRank: 0 };
  }

  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  getSnapshot = () => this.version;

  _notify() {
    if (this.pendingNotify) return;
    this.pendingNotify = true;
    setTimeout(() => {
      this.pendingNotify = false;
      this.version += 1;
      for (const listener of this.listeners) listener();
    }, this.flushMs);
  }

  /** Apply one `node_state` frame from the stream. */
  applyUpdate(update) {
    const state = update.state;
    if (!state) return;
    this.lastUpdateAt = performance.now();
    this.counters.updates += 1;

    if (state.severity === "none") {
      this.states.delete(state.node_id);
    } else {
      this.states.set(state.node_id, state);
    }
    if (update.alert) {
      this.counters.alerts += 1;
      this.feed.unshift({ ...update.alert, receivedAt: Date.now() / 1000 });
      if (this.feed.length > this.maxFeed) this.feed.length = this.maxFeed;
    }
    // The scene is updated synchronously; React catches up on the next flush.
    this.onSceneUpdate?.(state.node_id, state);
    this._notify();
  }

  applySnapshot(frame) {
    this.states.clear();
    for (const state of frame.states || []) {
      if (state.severity !== "none") this.states.set(state.node_id, state);
    }
    this.onSceneUpdate?.(null, null);
    this._notify();
  }

  /** Replace everything (history scrubbing). */
  setStates(states) {
    this.states = new Map(states.map((s) => [s.node_id, s]));
    this.onSceneUpdate?.(null, null);
    this._notify();
  }

  clear() {
    this.states.clear();
    this.feed = [];
    this.onSceneUpdate?.(null, null);
    this._notify();
  }

  /** Alerting nodes, worst first - the order the operator should work in. */
  ranked() {
    const rank = { none: 0, info: 1, low: 2, medium: 3, high: 4, critical: 5 };
    return [...this.states.values()].sort(
      (a, b) => rank[b.severity] - rank[a.severity] || (b.last_alert_at || 0) - (a.last_alert_at || 0)
    );
  }

  worst() {
    return this.ranked()[0] || null;
  }
}
