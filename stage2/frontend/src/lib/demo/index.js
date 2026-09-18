/**
 * Demo transport: the same client surface as `MapApi` / `MapStream`, backed by
 * `DemoEngine` running in the page instead of by HTTP and a WebSocket.
 *
 * Every method returns the same shape the real endpoint returns, because the
 * components consuming them are shared. Where the live client awaits a network
 * round trip, this resolves a promise — which is the only behavioural
 * difference the UI can observe, and it is in the harmless direction.
 */
import { expandTopology } from "../api.js";
import { DemoEngine } from "./engine.js";

const DEMO_TOPOLOGY_URL = `${import.meta.env.BASE_URL || "/"}demo-topology.json`;

/** One engine per page; both the API and the stream read from it. */
let enginePromise = null;
const listeners = new Set();

async function getEngine() {
  if (enginePromise) return enginePromise;
  enginePromise = (async () => {
    const response = await fetch(DEMO_TOPOLOGY_URL);
    if (!response.ok) {
      throw new Error(`demo topology missing (${response.status}) — run "npm run build:demo-data"`);
    }
    const topology = expandTopology(await response.json());
    const engine = new DemoEngine(topology, {
      onUpdate: (frame) => { for (const listener of listeners) listener(frame); },
    });
    engine.start();
    return { engine, topology };
  })();
  return enginePromise;
}

export class DemoApi {
  constructor() {
    this.apiKey = "demo";
    this._exportCache = null;
  }

  async health() {
    const { engine, topology } = await getEngine();
    return {
      status: "ok", mode: "demo", uptime_s: engine.stats().uptime_s,
      topology_version: topology.version, nodes: topology.nodes.length,
      redis: false,
      connector: { connected: false, running: false, mode: "in-browser simulator" },
      auth: { enabled: false, mode: "none (static demo)" },
    };
  }

  async topology() {
    return (await getEngine()).topology;
  }

  async states() {
    const { engine } = await getEngine();
    const states = engine.getStates();
    return { ts: Date.now() / 1000, count: states.length, states };
  }

  async node(id) {
    const { engine, topology } = await getEngine();
    const index = topology.index.get(id);
    if (index === undefined) throw new Error(`no such node: ${id}`);
    const node = topology.nodes[index];

    const neighbours = [];
    for (const link of topology.links) {
      if (link.source === id) neighbours.push({ id: link.target, kind: link.kind });
      else if (link.target === id) neighbours.push({ id: link.source, kind: link.kind });
    }
    const state = engine.getStates().find((s) => s.node_id === id) || null;
    const alerts = engine.alerts.filter((a) => a.node_id === id).slice(0, 25);

    return {
      node: {
        ...node,
        ips: node.ip ? [node.ip] : [],
        discovered_by: node.tags?.includes("infrastructure") ? ["snmp", "lldp"] : ["arp"],
        services: [],
        first_seen: engine.startedAt, last_seen: Date.now() / 1000,
      },
      state,
      neighbours,
      alerts,
      telemetry: {
        alert_count: state?.alert_count ?? 0,
        alert_count_window: state?.alert_count_window ?? 0,
        top_ports: state?.top_ports ?? [],
        top_peers: state?.top_peers ?? [],
        services: [],
        discovered_by: ["demo"],
      },
    };
  }

  async alerts({ limit = 100, minSeverity = "info", nodeId = null } = {}) {
    const { engine } = await getEngine();
    const rank = { info: 1, low: 2, medium: 3, high: 4, critical: 5 };
    const floor = rank[minSeverity] ?? 1;
    const alerts = engine.alerts
      .filter((a) => (nodeId ? a.node_id === nodeId : (rank[a.severity] ?? 0) >= floor))
      .slice(0, limit);
    return { count: alerts.length, alerts };
  }

  async timeline({ start = null, end = null, limit = 5000 } = {}) {
    const { engine } = await getEngine();
    let events = engine.timeline;
    if (start != null) events = events.filter((e) => e.ts >= start);
    if (end != null) events = events.filter((e) => e.ts <= end);
    events = events.slice(-limit);
    return {
      count: events.length,
      span: events.length ? { start: events[0].ts, end: events[events.length - 1].ts } : null,
      events,
    };
  }

  async replay(ts) {
    const { engine } = await getEngine();
    const states = engine.replayAt(ts);
    return { ts, count: states.length, states };
  }

  async stats() {
    return (await getEngine()).engine.stats();
  }

  async acknowledge(nodeId) {
    const { engine } = await getEngine();
    const state = engine.acknowledge(nodeId);
    if (!state) throw new Error(`node ${nodeId} has no active state`);
    return { acknowledged: true, node_id: nodeId, by: "demo" };
  }

  async createTicket(nodeId) {
    const { engine, topology } = await getEngine();
    const index = topology.index.get(nodeId);
    const node = index !== undefined ? topology.nodes[index] : null;
    const state = engine.getStates().find((s) => s.node_id === nodeId);
    if (!node || !state) throw new Error("no active alert state for this host");
    // The live build posts to a webhook stub that logs what it would have sent.
    // Here there is nothing to post to, so the dry run is the whole story.
    return {
      status: "dry-run",
      ticket_ref: `DEMO-${Math.floor(Date.now() / 1000) % 100000}`,
      http_status: null, error: "",
    };
  }

  async simulateIncident({ nodeId = null, scenario = null } = {}) {
    const { engine } = await getEngine();
    const injected = engine.incident({ nodeId, scenario });
    return { ...injected, injected_at: Date.now() / 1000, updates: injected.alerts };
  }

  /**
   * Export as a blob URL rather than a server route.
   *
   * Cached against the timeline length so a re-render does not mint a new blob
   * every frame, and the previous URL is revoked when it does change.
   */
  exportUrl(format = "csv") {
    const engine = this._engine;
    if (!engine) {
      getEngine().then(({ engine: e }) => { this._engine = e; });
      return "#";
    }
    const key = `${format}:${engine.timeline.length}`;
    if (this._exportCache?.key === key) return this._exportCache.url;
    if (this._exportCache) URL.revokeObjectURL(this._exportCache.url);

    let body;
    let type;
    if (format === "csv") {
      const header = "timestamp_iso,timestamp_epoch,node_id,severity,score,src_ip,dst_ip,dst_port,flow_id";
      const rows = engine.timeline.map((e) => [
        new Date(e.ts * 1000).toISOString(), e.ts.toFixed(3), e.node_id, e.severity,
        e.score.toFixed(4), e.src_ip, e.dst_ip, e.dst_port, e.flow_id,
      ].join(","));
      body = [header, ...rows].join("\n");
      type = "text/csv";
    } else {
      body = JSON.stringify(
        { exported_at: Date.now() / 1000, count: engine.timeline.length, events: engine.timeline },
        null, 2
      );
      type = "application/json";
    }
    const url = URL.createObjectURL(new Blob([body], { type }));
    this._exportCache = { key, url };
    return url;
  }
}

/** Same contract as `MapStream`, driven by the engine's update callback. */
export class DemoStream {
  constructor({ onMessage, onStatus } = {}) {
    this.onMessage = onMessage || (() => {});
    this.onStatus = onStatus || (() => {});
    this.received = 0;
    this.lastMessageAt = 0;
    this.closed = false;
  }

  async connect() {
    this.onStatus({ state: "connecting", attempt: 0 });
    const { engine } = await getEngine();
    if (this.closed) return;

    this._listener = (frame) => {
      this.received += 1;
      this.lastMessageAt = performance.now();
      this.onMessage(frame);
    };
    listeners.add(this._listener);
    this.onStatus({ state: "open", attempt: 0 });

    // Prime exactly as the server's first WebSocket frame does.
    this.onMessage({
      type: "snapshot", ts: Date.now() / 1000,
      topology_version: engine.topology.version, states: engine.getStates(),
    });
  }

  close() {
    this.closed = true;
    if (this._listener) listeners.delete(this._listener);
    this.onStatus({ state: "closed" });
  }
}

export const DEMO_MODE = String(import.meta.env.VITE_DEMO_MODE || "") === "1";
