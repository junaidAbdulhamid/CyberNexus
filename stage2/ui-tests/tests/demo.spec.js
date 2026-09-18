import { expect, test } from "@playwright/test";

/**
 * The static demo build: no backend, no Redis, no WebSocket.
 *
 * This is what gets deployed to a static host, and it is a genuinely different
 * build target — a different transport, a different data source and a different
 * entry path through `App`. So it is tested rather than assumed: the checks
 * below are deliberately the same user journeys the live suite covers, proving
 * the swap is invisible above the transport boundary.
 *
 * Run it against a preview server:
 *   cd frontend && npm run preview:demo
 *   CN_DEMO_URL=http://localhost:4173 npx playwright test tests/demo.spec.js
 *
 * Skipped automatically when that server is not up, so it never fails CI for
 * being unconfigured.
 */
const DEMO_URL = process.env.CN_DEMO_URL || "http://localhost:4173";

async function demoAvailable() {
  try {
    const response = await fetch(`${DEMO_URL}/demo-topology.json`, {
      signal: AbortSignal.timeout(2000),
    });
    return response.ok;
  } catch {
    return false;
  }
}

let available = false;
test.beforeAll(async () => { available = await demoAvailable(); });
test.beforeEach(async ({ page }) => {
  test.skip(!available, `no demo preview server at ${DEMO_URL} (npm run preview:demo)`);
  await page.goto(DEMO_URL);
  await page.waitForFunction(() => window.__cnmap && window.__cnmap.getTopologySize() > 0,
    null, { timeout: 30_000 });
  await page.waitForFunction(
    () => document.querySelector(".statusbar .dot")?.className.includes("dot-open"),
    null, { timeout: 20_000 });
});

test.describe("static demo build", () => {
  test("loads the topology and renders it with no backend", async ({ page }) => {
    const requests = [];
    page.on("request", (r) => {
      const url = new URL(r.url());
      if (url.pathname.startsWith("/api") || url.protocol.startsWith("ws")) requests.push(r.url());
    });
    await page.waitForTimeout(3000);

    expect(await page.evaluate(() => window.__cnmap.getTopologySize())).toBeGreaterThan(300);
    const stats = await page.evaluate(() => window.__cnmap.getSceneStats());
    expect(stats.nodesRendered).toBeGreaterThan(300);
    // The whole point: nothing is talking to a server.
    expect(requests, "demo build must not call the backend").toEqual([]);
  });

  test("labels itself as demo data rather than implying a live sensor", async ({ page }) => {
    await expect(page.locator(".demo-badge")).toBeVisible();
  });

  test("the in-browser simulator produces a live, changing incident feed", async ({ page }) => {
    await expect
      .poll(() => page.evaluate(() => window.__cnmap.getAlertingNodes().length), { timeout: 20_000 })
      .toBeGreaterThan(0);

    const before = await page.evaluate(() => window.__cnmap.getCounters().alerts);
    await page.waitForTimeout(6000);
    const after = await page.evaluate(() => window.__cnmap.getCounters().alerts);
    expect(after, "alerts must keep arriving").toBeGreaterThan(before);
  });

  test("selecting a host opens its detail panel", async ({ page }) => {
    const worst = await page.evaluate(() => window.__cnmap.getWorst());
    expect(worst).toBeTruthy();
    await page.getByTestId(`alert-row-${worst.node_id}`).click();
    const detail = page.getByTestId("node-detail");
    await expect(detail).toBeVisible();
    await expect(detail).toHaveAttribute("data-node-id", worst.node_id);
    await expect(detail.locator(".alert-table tbody tr").first()).toBeVisible();
  });

  test("acknowledge, ticket and the command palette all work offline", async ({ page }) => {
    const worst = await page.evaluate(() => window.__cnmap.getWorst());
    await page.getByTestId(`alert-row-${worst.node_id}`).click();
    await page.getByTestId("create-ticket").click();
    await expect(page.getByTestId("toast")).toBeVisible();

    await page.keyboard.press("Escape");
    await page.keyboard.press("ControlOrMeta+k");
    await expect(page.getByTestId("command-palette")).toBeVisible();
    await page.getByTestId("palette-input").fill("hq-");
    await expect(page.locator(".palette-item").first()).toBeVisible();
    await page.keyboard.press("Escape");

    await page.evaluate((id) => window.__cnmap.selectNode(id), worst.node_id);
    await page.keyboard.press("a");
    await expect
      .poll(() => page.evaluate((id) =>
        window.__cnmap.getAlertingNodes().some((s) => s.node_id === id), worst.node_id))
      .toBe(false);
  });

  test("the log view and history scrubber are populated", async ({ page }) => {
    await page.waitForTimeout(2500);
    await page.getByTestId("view-log").click();
    await expect(page.locator(".log-table tbody tr").first()).toBeVisible();
    await page.getByTestId("view-map").click();
    await expect(page.locator(".timeline-density .density-bar").first()).toBeAttached();
  });
});
