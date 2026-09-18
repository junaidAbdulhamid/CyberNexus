import { expect, test } from "@playwright/test";
import { clearAlerts, getTopology, injectIncident, openMap, pickHost } from "./helpers.js";

/**
 * Accessibility is a requirement here, not a nicety: a 3D canvas is opaque to
 * assistive technology, so the non-visual paths have to be tested as carefully
 * as the visual one.
 */

test.describe("accessibility", () => {
  test.beforeEach(async ({ page, request }) => {
    await openMap(page);
    await clearAlerts(request, page);
  });

  test("a keyboard user can reach and select an alerting host", async ({ page, request }) => {
    const topology = await getTopology(request);
    const target = pickHost(topology, 2);
    await injectIncident(request, { nodeId: target.id, scenario: "port_scan" });
    await expect(page.getByTestId(`alert-row-${target.id}`)).toBeVisible();

    await page.locator("body").click({ position: { x: 5, y: 5 } });
    await page.keyboard.press("w");                    // jump to worst alert
    await expect.poll(() => page.evaluate(() => window.__cnmap.getSelected()))
      .toBe(target.id);

    await page.keyboard.press("Escape");
    await expect.poll(() => page.evaluate(() => window.__cnmap.getSelected())).toBeNull();

    await page.keyboard.press("ArrowDown");            // next alerting host
    await expect.poll(() => page.evaluate(() => window.__cnmap.getSelected()))
      .not.toBeNull();
  });

  test("new alerts are announced to assistive technology", async ({ page, request }) => {
    const topology = await getTopology(request);
    const target = pickHost(topology, 4);
    await injectIncident(request, { nodeId: target.id, scenario: "exfiltration" });

    // Critical/high severity uses role=alert (assertive); anything lower is polite.
    const assertive = page.getByTestId("live-region-assertive");
    await expect(assertive).toContainText(/severity alert on/i, { timeout: 8000 });
    await expect(assertive).toContainText(target.name);
  });

  test("the canvas has a text alternative and a keyboard-reachable equivalent", async ({ page, request }) => {
    const canvas = page.getByTestId("map-canvas");
    await expect(canvas).toHaveAttribute("role", "img");
    const label = await canvas.getAttribute("aria-label");
    expect(label).toMatch(/keyboard/i);

    const topology = await getTopology(request);
    const target = pickHost(topology, 6);
    await injectIncident(request, { nodeId: target.id, scenario: "c2_beacon" });

    const list = page.getByRole("list", { name: /alerting hosts/i });
    await expect(list).toBeAttached();
    await expect(list.getByRole("button").first()).toContainText(/severity on/i);
  });

  test("the skip link works and the shortcut dialog is a real dialog", async ({ page }) => {
    await page.keyboard.press("Tab");
    const skip = page.getByRole("link", { name: /skip to map/i });
    await expect(skip).toBeFocused();

    await page.locator("body").click({ position: { x: 5, y: 5 } });
    await page.keyboard.press("?");
    const dialog = page.getByRole("dialog", { name: /keyboard shortcuts/i });
    await expect(dialog).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(dialog).not.toBeVisible();
  });

  test("palettes switch and persist, including high contrast", async ({ page }) => {
    await page.getByLabel("Colour palette").selectOption("colorblind");
    await expect(page.locator(".app")).toHaveAttribute("data-palette", "colorblind");
    const cbBackground = await page.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue("--sev-critical").trim());

    await page.getByLabel("Colour palette").selectOption("highContrast");
    await expect(page.locator(".app")).toHaveAttribute("data-palette", "highContrast");

    await page.reload();
    await page.waitForFunction(() => window.__cnmap?.getTopologySize() > 0);
    await expect(page.locator(".app")).toHaveAttribute("data-palette", "highContrast");
    expect(cbBackground).not.toBe("");
  });

  test("severity is encoded by more than colour", async ({ page, request }) => {
    const topology = await getTopology(request);
    const target = pickHost(topology, 8);
    await injectIncident(request, { nodeId: target.id, scenario: "exfiltration" });
    const row = page.getByTestId(`alert-row-${target.id}`);
    await expect(row).toBeVisible();
    // Text label and glyph, so the row is unambiguous in greyscale.
    await expect(row.locator(".alert-row-sev")).toHaveText(/critical|high/);
    await expect(row.locator(".sev-glyph")).toHaveText(/!+/);
  });

  test("every interactive control has an accessible name", async ({ page }) => {
    // Collected in one DOM pass rather than by iterating locators: the sidebar
    // is live, and a list that grows mid-iteration makes the check flaky for
    // reasons that have nothing to do with accessibility.
    const unnamed = await page.evaluate(() => {
      const selector = ".sidebar button, .sidebar input, .sidebar select,"
        + " .topbar button, .topbar select, .incident-banner button";
      const controls = [...document.querySelectorAll(selector)];
      const nameOf = (el) => (
        el.getAttribute("aria-label")
        || el.getAttribute("title")
        || el.getAttribute("placeholder")
        || (el.labels && el.labels.length ? el.labels[0].textContent : "")
        || el.textContent
        || ""
      ).trim();
      return {
        total: controls.length,
        missing: controls.filter((el) => !nameOf(el)).map((el) =>
          `${el.tagName.toLowerCase()}.${el.className}`),
      };
    });
    expect(unnamed.total).toBeGreaterThan(5);
    expect(unnamed.missing, "controls without an accessible name").toEqual([]);
  });
});
