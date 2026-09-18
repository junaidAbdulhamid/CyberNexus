import { defineConfig, devices } from "@playwright/test";

/**
 * The suite drives a real browser against a running stack, so it needs the
 * backend on :8080 with the built frontend. `webServer` starts it if it is not
 * already up.
 *
 * `channel: "chrome"` uses the Chrome already installed on the machine rather
 * than downloading Playwright's bundled build. WebGL is the reason: a headless
 * browser without a GPU falls back to SwiftShader, which renders correctly but
 * slowly, and these tests assert on what is actually drawn.
 */
const BASE_URL = process.env.CN_MAP_URL || "http://localhost:8080";

export default defineConfig({
  testDir: "./tests",
  timeout: 60_000,
  expect: { timeout: 15_000, toHaveScreenshot: { maxDiffPixelRatio: 0.03, animations: "disabled" } },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"], ["json", { outputFile: "results/report.json" }]],
  outputDir: "results/artifacts",
  use: {
    baseURL: BASE_URL,
    channel: "chrome",
    headless: process.env.CN_HEADED ? false : true,
    viewport: { width: 1440, height: 900 },
    launchOptions: {
      // WebGL in headless Chrome needs these; without them the canvas context
      // creation fails and every map test reports a false negative.
      args: [
        "--enable-unsafe-swiftshader",
        "--use-gl=angle",
        "--enable-webgl",
        "--ignore-gpu-blocklist",
      ],
    },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: process.env.CN_NO_WEBSERVER ? undefined : {
    command: "bash ../scripts/start-backend.sh",
    url: `${BASE_URL}/api/health`,
    reuseExistingServer: true,
    timeout: 60_000,
    stdout: "pipe",
    stderr: "pipe",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
