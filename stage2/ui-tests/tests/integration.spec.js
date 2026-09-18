import { expect, test } from "@playwright/test";
import { apiHeaders, clearAlerts, getTopology, injectIncident, openMap, pickHost } from "./helpers.js";

/**
 * The acceptance test for the brief's first criterion: a Stage 1 alert reaches
 * the browser and visibly marks the right node, in under a second.
 *
 * "Visibly" is asserted three ways, deliberately, because each alone is weak:
 *  1. application state - the node is in the alerting set;
 *  2. the DOM - an alert row exists with the right severity;
 *  3. the rendered canvas - pixels actually changed.
 * A test that only checked (1) would pass even if the renderer were broken.
 */

test.describe("Stage 1 alert -> node highlighted", () => {
  test.beforeEach(async ({ request, page }) => {
    await openMap(page);
    await clearAlerts(request, page);
  });

  test("an injected incident marks the correct node in the UI", async ({ page, request }) => {
    const topology = await getTopology(request);
    const target = pickHost(topology, 3);

    const injected = await injectIncident(request, { nodeId: target.id, scenario: "c2_beacon" });
    expect(injected.node_id).toBe(target.id);

    // 1. application state
    await page.waitForFunction(
      (id) => (window.__cnmap?.getAlertingNodes() || []).some((s) => s.node_id === id),
      target.id, { timeout: 5000 }
    );
    const alerting = await page.evaluate(() => window.__cnmap.getAlertingNodes());
    const state = alerting.find((s) => s.node_id === target.id);
    expect(state).toBeTruthy();
    expect(["high", "critical"]).toContain(state.severity);

    // 2. DOM
    const row = page.getByTestId(`alert-row-${target.id}`);
    await expect(row).toBeVisible();
    await expect(row).toHaveAttribute("data-severity", /high|critical/);
    await expect(row).toContainText(target.name);

    // 3. detail panel opens on the right host
    await row.click();
    const detail = page.getByTestId("node-detail");
    await expect(detail).toBeVisible();
    await expect(detail).toHaveAttribute("data-node-id", target.id);
  });

  test("the rendered canvas changes when a node starts alerting", async ({ page, request }) => {
    const topology = await getTopology(request);
    const target = pickHost(topology, 7);
    const canvas = page.getByTestId("map-canvas");
    await page.waitForTimeout(1200);          // let the scene settle
    const before = await canvas.screenshot();

    await injectIncident(request, { nodeId: target.id, scenario: "exfiltration" });
    await page.waitForFunction(
      (id) => (window.__cnmap?.getAlertingNodes() || []).some((s) => s.node_id === id),
      target.id, { timeout: 5000 }
    );
    await page.waitForTimeout(600);           // one pulse cycle
    const after = await canvas.screenshot();

    expect(Buffer.compare(before, after)).not.toBe(0);
  });

  test("alert-to-visible latency is under one second", async ({ page, request }) => {
    const topology = await getTopology(request);
    const samples = [];

    for (let i = 0; i < 5; i += 1) {
      const target = pickHost(topology, 20 + i * 11);
      await page.evaluate(() => { window.__cnLatency = null; });
      const t0 = Date.now();
      await injectIncident(request, { nodeId: target.id, scenario: "brute_force" });
      await page.waitForFunction(
        (id) => (window.__cnmap?.getAlertingNodes() || []).some((s) => s.node_id === id),
        target.id, { timeout: 5000 }
      );
      samples.push(Date.now() - t0);
      await clearAlerts(request, page);
    }

    const max = Math.max(...samples);
    const median = [...samples].sort((a, b) => a - b)[Math.floor(samples.length / 2)];
    console.log(`alert->visible latency: median ${median} ms, max ${max} ms, samples ${samples.join("/")}`);
    expect(max).toBeLessThan(1000);
  });

  test("alerting nodes stay visible even when filtered out", async ({ page, request }) => {
    const topology = await getTopology(request);
    const target = pickHost(topology, 5);
    await injectIncident(request, { nodeId: target.id, scenario: "port_scan" });
    await expect(page.getByTestId(`alert-row-${target.id}`)).toBeVisible();

    // A filter that excludes everything must not hide the incident.
    await page.getByLabel("Search").fill("zzz-no-such-host-zzz");
    await page.waitForTimeout(400);
    const stillRendered = await page.evaluate((id) => {
      const scene = window.__cnmap.getSceneStats();
      const alerting = window.__cnmap.getAlertingNodes().some((s) => s.node_id === id);
      return { alerting, nodesRendered: scene.nodesRendered };
    }, target.id);
    expect(stillRendered.alerting).toBe(true);
    expect(stillRendered.nodesRendered).toBeGreaterThan(0);
    await expect(page.getByTestId(`alert-row-${target.id}`)).toBeVisible();
  });

  test("the log view shows the same alert as the map", async ({ page, request }) => {
    const topology = await getTopology(request);
    const target = pickHost(topology, 9);
    await injectIncident(request, { nodeId: target.id, scenario: "lateral_movement" });
    await expect(page.getByTestId(`alert-row-${target.id}`)).toBeVisible();

    await page.getByTestId("view-log").click();
    await expect(page.getByTestId("log-view")).toBeVisible();
    await expect(page.getByTestId(`log-node-${target.id}`).first()).toBeVisible();
  });

  test("ticket creation reaches the webhook stub", async ({ page, request }) => {
    const topology = await getTopology(request);
    const target = pickHost(topology, 11);
    await injectIncident(request, { nodeId: target.id, scenario: "exfiltration" });
    await page.getByTestId(`alert-row-${target.id}`).click();
    await page.getByTestId("create-ticket").click();

    await expect.poll(async () => {
      const response = await request.get("/api/integrations/webhook", { headers: apiHeaders() });
      const body = await response.json();
      return body.deliveries.some((d) => d.node_id === target.id);
    }, { timeout: 10_000 }).toBe(true);
  });

  test("the map sustains an interactive frame rate", async ({ page, request }) => {
    await injectIncident(request, { scenario: "port_scan", noise: 40 });
    await page.waitForTimeout(3000);
    const stats = await page.evaluate(() => window.__cnmap.getSceneStats());
    console.log(`scene: ${stats.fps.toFixed(1)} fps, ${stats.drawCalls} draw calls, ` +
                `${stats.nodesRendered} nodes rendered`);
    expect(stats.fps).toBeGreaterThan(20);
    // The whole point of instancing: node count must not drive draw calls.
    expect(stats.drawCalls).toBeLessThan(25);
  });
});
