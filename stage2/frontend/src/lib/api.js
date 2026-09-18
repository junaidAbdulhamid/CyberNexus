/**
 * Backend client: REST snapshots plus the live WebSocket stream.
 *
 * The split mirrors the server's: fetch the topology once (it is big and rarely
 * changes, and the ETag makes a re-fetch nearly free), then take every
 * subsequent change as a delta off the socket. The socket reconnects with
 * exponential backoff and re-primes itself from the server's snapshot frame, so
 * a dropped connection costs a second of staleness rather than a page reload.
 */

const DEFAULT_KEY = import.meta.env?.VITE_API_KEY || "demo-key";

/**
 * Where the backend lives. Empty means same origin, which is the case when the
 * FastAPI service serves the built bundle itself (`docker compose up`).
 *
 * Set `VITE_API_BASE=https://api.example.com` at build time to split the
 * deployment — a static frontend on one host, the backend on another. The
 * backend must then allow that origin via `CN_MAP_CORS`.
 */
const DEFAULT_BASE = import.meta.env?.VITE_API_BASE || "";

/**
 * `VITE_DEMO_MODE=1` builds a static bundle with no backend at all: the same
 * client interfaces are served by an in-browser engine instead. See
 * `lib/demo/engine.js` for what that does and does not reproduce.
 */
export const DEMO_MODE = String(import.meta.env?.VITE_DEMO_MODE || "") === "1";

/** Resolve the transport once, at startup. */
export async function createClients() {
  if (DEMO_MODE) {
    const demo = await import("./demo/index.js");
    return { api: new demo.DemoApi(), Stream: demo.DemoStream, mode: "demo" };
  }
  return { api: new MapApi(), Stream: MapStream, mode: "live" };
}

export class MapApi {
  constructor({ baseUrl = DEFAULT_BASE, apiKey = DEFAULT_KEY } = {}) {
    this.baseUrl = baseUrl;
    this.apiKey = apiKey;
    this.topologyEtag = null;
    this.topologyCache = null;
  }

  headers(extra = {}) {
    return { "X-API-Key": this.apiKey, ...extra };
  }

  async get(path, { etag = null } = {}) {
    const headers = this.headers();
    if (etag) headers["If-None-Match"] = etag;
    const response = await fetch(`${this.baseUrl}${path}`, { headers });
    if (response.status === 304) return { notModified: true };
    if (!response.ok) {
      throw new Error(`GET ${path} failed: ${response.status} ${response.statusText}`);
    }
    return { data: await response.json(), etag: response.headers.get("etag") };
  }

  async post(path, body) {
    const response = await fetch(`${this.baseUrl}${path}`, {
      method: "POST",
      headers: this.headers({ "Content-Type": "application/json" }),
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!response.ok) {
      throw new Error(`POST ${path} failed: ${response.status}`);
    }
    return response.json();
  }

  async health() {
    return (await this.get("/api/health")).data;
  }

  /** Compact columnar topology, expanded into objects once, here. */
  async topology() {
    const result = await this.get("/api/topology?format=compact", { etag: this.topologyEtag });
    if (result.notModified && this.topologyCache) return this.topologyCache;
    this.topologyEtag = result.etag;
    this.topologyCache = expandTopology(result.data);
    return this.topologyCache;
  }

  async states() {
    return (await this.get("/api/states")).data;
  }

  async node(id) {
    return (await this.get(`/api/nodes/${encodeURIComponent(id)}`)).data;
  }

  async alerts({ limit = 100, minSeverity = "info", nodeId = null } = {}) {
    const params = new URLSearchParams({ limit: String(limit), min_severity: minSeverity });
    if (nodeId) params.set("node_id", nodeId);
    return (await this.get(`/api/alerts?${params}`)).data;
  }

  async timeline({ start = null, end = null, limit = 5000 } = {}) {
    const params = new URLSearchParams({ limit: String(limit) });
    if (start) params.set("start", String(start));
    if (end) params.set("end", String(end));
    return (await this.get(`/api/timeline?${params}`)).data;
  }

  async replay(ts) {
    return (await this.get(`/api/replay?ts=${ts}`)).data;
  }

  async stats() {
    return (await this.get("/api/stats")).data;
  }

  acknowledge(nodeId) {
    return this.post(`/api/nodes/${encodeURIComponent(nodeId)}/acknowledge`);
  }

  createTicket(nodeId) {
    return this.post(`/api/nodes/${encodeURIComponent(nodeId)}/ticket`);
  }

  simulateIncident({ nodeId = null, scenario = null, noise = 0 } = {}) {
    const params = new URLSearchParams();
    if (nodeId) params.set("node_id", nodeId);
    if (scenario) params.set("scenario", scenario);
    if (noise) params.set("noise", String(noise));
    return this.post(`/api/simulate/incident?${params}`);
  }

  exportUrl(format = "csv") {
    return `${this.baseUrl}/api/timeline/export?format=${format}&api_key=${encodeURIComponent(this.apiKey)}`;
  }
}

/** Columnar arrays -> objects, done once per topology load. */
export function expandTopology(compact) {
  const fields = compact.node_fields;
  const nodes = compact.nodes.map((row) => {
    const node = {};
    fields.forEach((name, i) => { node[name] = row[i]; });
    node.position = { x: node.x, y: node.y, z: node.z };
    return node;
  });
  const links = compact.links.map(([source, target, kind]) => ({
    source: nodes[source]?.id, target: nodes[target]?.id, kind,
    sourceIndex: source, targetIndex: target,
  }));
  return {
    version: compact.version,
    nodes,
    links,
    bounds: compact.bounds,
    stats: compact.stats,
    index: new Map(nodes.map((n, i) => [n.id, i])),
  };
}

/**
 * Live stream with reconnect.
 * Frames: {type:"snapshot"|"node_state"|"batch"|"topology_changed"|"pong"}
 */
export class MapStream {
  constructor({ baseUrl = DEFAULT_BASE, apiKey = DEFAULT_KEY, onMessage, onStatus } = {}) {
    this.apiKey = apiKey;
    this.onMessage = onMessage || (() => {});
    this.onStatus = onStatus || (() => {});
    const httpBase = baseUrl || window.location.origin;
    this.url = `${httpBase.replace(/^http/, "ws")}/ws/stream?api_key=${encodeURIComponent(apiKey)}`;
    this.socket = null;
    this.attempt = 0;
    this.closed = false;
    this.received = 0;
    this.lastMessageAt = 0;
  }

  connect() {
    this.closed = false;
    this.onStatus({ state: "connecting", attempt: this.attempt });
    const socket = new WebSocket(this.url);
    this.socket = socket;

    socket.onopen = () => {
      this.attempt = 0;
      this.onStatus({ state: "open" });
      this.heartbeat = setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) socket.send("ping");
      }, 15000);
    };
    socket.onmessage = (event) => {
      this.received += 1;
      this.lastMessageAt = performance.now();
      let frame;
      try {
        frame = JSON.parse(event.data);
      } catch {
        return;
      }
      if (frame.type === "batch") {
        for (const update of frame.updates) this.onMessage(update);
      } else {
        this.onMessage(frame);
      }
    };
    socket.onclose = () => {
      clearInterval(this.heartbeat);
      this.onStatus({ state: "closed" });
      if (!this.closed) this.scheduleReconnect();
    };
    socket.onerror = () => this.onStatus({ state: "error" });
  }

  scheduleReconnect() {
    this.attempt += 1;
    const delay = Math.min(500 * 2 ** (this.attempt - 1), 10000);
    this.onStatus({ state: "reconnecting", delay, attempt: this.attempt });
    setTimeout(() => { if (!this.closed) this.connect(); }, delay);
  }

  close() {
    this.closed = true;
    clearInterval(this.heartbeat);
    this.socket?.close();
  }
}
