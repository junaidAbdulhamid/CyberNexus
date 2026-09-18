/** Shared helpers: auth, incident injection, and waiting on the live stream. */

export const API_KEY = process.env.CN_MAP_API_KEY || "demo-key";

export function apiHeaders() {
  return { "X-API-Key": API_KEY };
}

/** Open the app and wait until the topology and the stream are both live. */
export async function openMap(page, { view = "map" } = {}) {
  await page.goto("/");
  await page.waitForFunction(() => window.__cnmap && window.__cnmap.getTopologySize() > 0, null,
    { timeout: 30_000 });
  await page.waitForSelector(".statusbar");
  await page.waitForFunction(
    () => document.querySelector(".statusbar .dot")?.className.includes("dot-open"),
    null, { timeout: 20_000 }
  );
  if (view === "log") await page.getByTestId("view-log").click();
  return page;
}

/** Inject a synthetic incident through the backend's real alert path. */
export async function injectIncident(request, { nodeId = null, scenario = "port_scan", noise = 0, seed = null } = {}) {
  const params = new URLSearchParams();
  if (nodeId) params.set("node_id", nodeId);
  if (scenario) params.set("scenario", scenario);
  if (noise) params.set("noise", String(noise));
  if (seed !== null) params.set("seed", String(seed));
  const response = await request.post(`/api/simulate/incident?${params}`, { headers: apiHeaders() });
  if (!response.ok()) throw new Error(`injection failed: ${response.status()}`);
  return response.json();
}

export async function getTopology(request) {
  const response = await request.get("/api/topology?format=compact", { headers: apiHeaders() });
  return response.json();
}

/** Pick a deterministic end host to target, by index into the workstation list. */
export function pickHost(topology, index = 0) {
  const fields = topology.node_fields;
  const idIdx = fields.indexOf("id");
  const typeIdx = fields.indexOf("device_type");
  const nameIdx = fields.indexOf("name");
  const hosts = topology.nodes.filter((row) =>
    ["workstation", "server", "iot", "printer"].includes(row[typeIdx]));
  const row = hosts[index % hosts.length];
  return { id: row[idIdx], name: row[nameIdx] };
}

export async function clearAlerts(request, page) {
  await page.evaluate(() => {
    const nodes = window.__cnmap?.getAlertingNodes() || [];
    return nodes.length;
  });
  // Acknowledging removes a node from the active set server-side.
  const states = await (await request.get("/api/states", { headers: apiHeaders() })).json();
  for (const state of states.states || []) {
    await request.post(`/api/nodes/${encodeURIComponent(state.node_id)}/acknowledge`,
      { headers: apiHeaders() });
  }
}
