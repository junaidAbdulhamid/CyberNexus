import { expect, test } from "@playwright/test";
import { clearAlerts, getTopology, injectIncident, openMap, pickHost } from "./helpers.js";

/**
 * Visual regression over the UI states that matter.
 *
 * The 3D canvas is masked out of every snapshot. It animates continuously
 * (pulsing halos, camera damping) and renders through whatever GPU the machine
 * has, so pixel-comparing it would produce a suite that fails for reasons
 * unrelated to the change under review. What *is* compared is the chrome around
 * it - the panels, the alert list, the legend, the timeline - which is where
 * visual regressions actually hurt and where the comparison is stable.
 *
 * Canvas rendering is covered instead by the pixel-change assertion in
 * `integration.spec.js`, which asks "did it change" rather than "does it match
 * these exact pixels".
 */

const MASK_CANVAS = (page) => [page.getByTestId("map-canvas"), page.locator(".statusbar")];

test.describe("visual regression", () => {
  test.beforeEach(async ({ page, request }) => {
    await openMap(page);
    await clearAlerts(request, page);
    await page.waitForTimeout(800);
  });

  test("idle map shell", async ({ page }) => {
    await expect(page).toHaveScreenshot("01-idle-map.png", {
      mask: MASK_CANVAS(page),
      fullPage: false,
    });
  });

  test("sidebar with active alerts", async ({ page, request }) => {
    const topology = await getTopology(request);
    // Seeded so the burst sizes and ports are identical every run; without it
    // the snapshot photographs a different incident each time.
    for (const [i, scenario] of ["port_scan", "exfiltration", "c2_beacon"].entries()) {
      await injectIncident(request, {
        nodeId: pickHost(topology, 30 + i * 13).id, scenario, seed: 1000 + i,
      });
    }
    await expect(page.locator(".alert-list li")).toHaveCount(3, { timeout: 10_000 });
    await page.waitForTimeout(500);
    await expect(page.locator(".sidebar")).toHaveScreenshot("02-sidebar-alerts.png");
  });

  test("node detail panel", async ({ page, request }) => {
    const topology = await getTopology(request);
    const target = pickHost(topology, 44);
    await injectIncident(request, { nodeId: target.id, scenario: "brute_force", seed: 2024 });
    await page.getByTestId(`alert-row-${target.id}`).click();
    const detail = page.getByTestId("node-detail");
    await expect(detail).toBeVisible();
    await page.waitForTimeout(400);
    await expect(detail).toHaveScreenshot("03-node-detail.png", {
      // Timestamps and scores move every run; the layout is what is under test.
      mask: [detail.locator(".alert-table"), detail.locator(".severity-banner")],
    });
  });

  test("alert log view", async ({ page, request }) => {
    const topology = await getTopology(request);
    await injectIncident(request, { nodeId: pickHost(topology, 12).id, scenario: "port_scan", seed: 3003 });
    await page.getByTestId("view-log").click();
    await expect(page.getByTestId("log-view")).toBeVisible();
    await page.waitForTimeout(600);
    await expect(page.locator(".logview-toolbar")).toHaveScreenshot("04-logview-toolbar.png");
  });

  test("high contrast palette", async ({ page, request }) => {
    const topology = await getTopology(request);
    await injectIncident(request, { nodeId: pickHost(topology, 17).id, scenario: "exfiltration", seed: 3001 });
    await page.getByLabel("Colour palette").selectOption("highContrast");
    await page.waitForTimeout(600);
    await expect(page.locator(".sidebar")).toHaveScreenshot("05-sidebar-high-contrast.png");
  });

  test("colourblind palette", async ({ page, request }) => {
    const topology = await getTopology(request);
    await injectIncident(request, { nodeId: pickHost(topology, 19).id, scenario: "c2_beacon", seed: 3002 });
    await page.getByLabel("Colour palette").selectOption("colorblind");
    await page.waitForTimeout(600);
    await expect(page.locator(".sidebar")).toHaveScreenshot("06-sidebar-colorblind.png");
  });

  test("status strip with a live incident", async ({ page, request }) => {
    const topology = await getTopology(request);
    await injectIncident(request, {
      nodeId: pickHost(topology, 23).id, scenario: "exfiltration", seed: 4100,
    });
    await expect(page.getByTestId("kpi-threat")).toContainText(/CRITICAL|HIGH/, { timeout: 8000 });
    await page.waitForTimeout(700);
    await expect(page.locator(".kpi-strip")).toHaveScreenshot("08-kpi-strip.png", {
      // The rate tile and the sparkline move on their own; the tile layout,
      // typography and threat meter are what this snapshot is guarding.
      mask: [page.locator(".kpi").nth(2), page.locator(".kpi-spark")],
    });
  });

  test("command palette", async ({ page, request }) => {
    const topology = await getTopology(request);
    await injectIncident(request, {
      nodeId: pickHost(topology, 31).id, scenario: "port_scan", seed: 4200,
    });
    await page.waitForTimeout(600);
    await page.locator("body").click({ position: { x: 5, y: 5 } });
    await page.keyboard.press("ControlOrMeta+k");
    const palette = page.getByTestId("command-palette");
    await expect(palette).toBeVisible();
    await page.getByTestId("palette-input").fill("acknowledge");
    await page.waitForTimeout(400);
    await expect(palette).toHaveScreenshot("09-command-palette.png");
  });

  test("incident banner", async ({ page, request }) => {
    const topology = await getTopology(request);
    await injectIncident(request, {
      nodeId: pickHost(topology, 37).id, scenario: "c2_beacon", seed: 4300,
    });
    const banner = page.getByTestId("incident-banner");
    await expect(banner).toBeVisible();
    await page.waitForTimeout(600);
    await expect(banner).toHaveScreenshot("10-incident-banner.png");
  });

  test("keyboard shortcut dialog", async ({ page }) => {
    await page.locator("body").click({ position: { x: 5, y: 5 } });
    await page.keyboard.press("?");
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await expect(dialog).toHaveScreenshot("07-shortcuts.png");
  });
});
