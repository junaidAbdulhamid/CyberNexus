/**
 * MTTI experiment harness.
 *
 * Runs N randomised trials of the same identification task in two interfaces -
 * the 3D map and the log view - measures what each interface actually presents,
 * and applies the documented cost model in `model.js` to predict identification
 * time.
 *
 * **What is measured** (real observations of the running system):
 *   - system latency from alert injection to the information being on screen;
 *   - how many items each interface presents at that moment;
 *   - where the target sits among them;
 *   - how many rows belong to the target host (the aggregation problem);
 *   - whether the target was below the fold and needed scrolling.
 *
 * **What is modelled**: the conversion of those observations into seconds.
 *
 * **What is not done**: human trials. No person was timed. The number this
 * prints is a model output, and `MTTI.md` gives the protocol for running the
 * real thing with people.
 *
 * Usage:
 *   node mtti/run-mtti.js --trials 30 --noise 60 --out results/mtti.json
 */
import fs from "node:fs";
import path from "node:path";
import { chromium } from "@playwright/test";
import {
  bootstrapImprovementCI, mean, median, modelLogCondition, modelMapCondition,
  sensitivity, stdev,
} from "./model.js";

const argv = process.argv.slice(2);
const arg = (name, fallback) => {
  const i = argv.indexOf(`--${name}`);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback;
};
const flag = (name) => argv.includes(`--${name}`);

const CONFIG = {
  baseUrl: arg("url", process.env.CN_MAP_URL || "http://localhost:8080"),
  apiKey: arg("api-key", process.env.CN_MAP_API_KEY || "demo-key"),
  trials: Number(arg("trials", 30)),
  noise: Number(arg("noise", 60)),
  seed: Number(arg("seed", 20260918)),
  out: arg("out", "results/mtti.json"),
  headed: flag("headed"),
  scenarios: ["port_scan", "c2_beacon", "exfiltration", "brute_force", "lateral_movement"],
};

/** Deterministic PRNG so a reported result can be reproduced exactly. */
function makeRng(seed) {
  let state = seed >>> 0;
  return () => {
    state ^= state << 13; state >>>= 0;
    state ^= state >> 17;
    state ^= state << 5; state >>>= 0;
    return state / 0xffffffff;
  };
}

const headers = { "X-API-Key": CONFIG.apiKey };

async function api(pathname, options = {}) {
  const response = await fetch(`${CONFIG.baseUrl}${pathname}`, { headers, ...options });
  if (!response.ok) throw new Error(`${pathname} -> ${response.status}`);
  return response.json();
}

async function clearAlerts() {
  const { states } = await api("/api/states");
  for (const state of states) {
    await api(`/api/nodes/${encodeURIComponent(state.node_id)}/acknowledge`, { method: "POST" });
  }
}

async function loadHosts() {
  const topology = await api("/api/topology?format=compact");
  const fields = topology.node_fields;
  const idIdx = fields.indexOf("id");
  const typeIdx = fields.indexOf("device_type");
  const nameIdx = fields.indexOf("name");
  return topology.nodes
    .filter((row) => ["workstation", "server", "iot", "printer"].includes(row[typeIdx]))
    .map((row) => ({ id: row[idIdx], name: row[nameIdx] }));
}

async function openApp(browser, view) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(CONFIG.baseUrl);
  await page.waitForFunction(() => window.__cnmap && window.__cnmap.getTopologySize() > 0,
    null, { timeout: 30_000 });
  await page.waitForFunction(
    () => document.querySelector(".statusbar .dot")?.className.includes("dot-open"),
    null, { timeout: 20_000 });
  if (view === "log") {
    await page.getByTestId("view-log").click();
    await page.getByTestId("log-view").waitFor();
  }
  return page;
}

/** Fill both interfaces with the same background noise before the incident. */
async function seedNoise(count) {
  if (count <= 0) return;
  await api(`/api/simulate/incident?scenario=port_scan&noise=${count}&node_id=`, { method: "POST" })
    .catch(() => {});
}

async function measureMapTrial(page, target, scenario) {
  await page.evaluate(() => { window.__mttiMark = performance.now(); });
  const injected = await api(
    `/api/simulate/incident?node_id=${encodeURIComponent(target.id)}&scenario=${scenario}`,
    { method: "POST" });

  const observed = await page.evaluate(async (id) => {
    const start = window.__mttiMark;
    // Wait for the node to be in the alerting set AND for a frame to have been
    // rendered after that - "on screen", not merely "in a variable".
    await new Promise((resolve, reject) => {
      const deadline = performance.now() + 10_000;
      const check = () => {
        const alerting = window.__cnmap.getAlertingNodes();
        if (alerting.some((s) => s.node_id === id)) {
          requestAnimationFrame(() => requestAnimationFrame(resolve));
        } else if (performance.now() > deadline) reject(new Error("timeout"));
        else setTimeout(check, 8);
      };
      check();
    });
    const alerting = window.__cnmap.getAlertingNodes();
    const rank = { info: 1, low: 2, medium: 3, high: 4, critical: 5 };
    const targetState = alerting.find((s) => s.node_id === id);
    // Did the incident banner name this host, with no interaction at all?
    // Verified from the rendered DOM rather than assumed.
    const banner = document.querySelector('[data-testid="incident-banner"]');
    const bannerNode = banner?.getAttribute("data-node-id") || null;
    const bannerText = banner?.textContent || "";
    return {
      bannerNodeId: bannerNode,
      bannerNamesTarget: bannerNode === id,
      bannerText: bannerText.slice(0, 160),
      latencyMs: performance.now() - start,
      alertingCount: alerting.length,
      // Distractors that compete visually: other nodes marked at the same or a
      // higher severity. Lower-severity nodes are dimmer and smaller, so they
      // do not compete for the eye.
      competingMarkedNodes: alerting.filter(
        (s) => s.node_id !== id && rank[s.severity] >= rank[targetState.severity]).length,
      targetSeverity: targetState.severity,
      targetRankInList: alerting.findIndex((s) => s.node_id === id),
      fps: window.__cnmap.getSceneStats()?.fps ?? null,
    };
  }, target.id);

  return {
    systemLatencyS: observed.latencyMs / 1000,
    identityPresentedWithoutInteraction: observed.bannerNamesTarget === true,
    bannerNodeId: observed.bannerNodeId,
    competingMarkedNodes: observed.competingMarkedNodes,
    alertingCount: observed.alertingCount,
    targetSeverity: observed.targetSeverity,
    targetRankInList: observed.targetRankInList,
    fps: observed.fps,
    alertsInjected: injected.alerts,
  };
}

async function measureLogTrial(page, target, scenario) {
  await page.evaluate(() => { window.__mttiMark = performance.now(); });
  const injected = await api(
    `/api/simulate/incident?node_id=${encodeURIComponent(target.id)}&scenario=${scenario}`,
    { method: "POST" });

  const observed = await page.evaluate(async (id) => {
    const start = window.__mttiMark;
    const selector = `[data-testid="log-row-${id}"]`;
    await new Promise((resolve, reject) => {
      const deadline = performance.now() + 15_000;
      const check = () => {
        if (document.querySelector(selector)) {
          requestAnimationFrame(() => requestAnimationFrame(resolve));
        } else if (performance.now() > deadline) reject(new Error("timeout"));
        else setTimeout(check, 16);
      };
      check();
    });

    const rows = [...document.querySelectorAll(".log-table tbody tr")];
    const targetRows = rows.filter((r) => r.getAttribute("data-testid") === `log-row-${id}`);
    const firstIndex = rows.findIndex((r) => r.getAttribute("data-testid") === `log-row-${id}`);
    const scroller = document.querySelector(".logview-scroll");
    const firstRow = targetRows[0];
    // Did the operator have to scroll to see it?
    const belowFold = firstRow
      ? firstRow.getBoundingClientRect().bottom > (scroller?.getBoundingClientRect().bottom ?? window.innerHeight)
      : false;

    return {
      latencyMs: performance.now() - start,
      rowsPresented: rows.length,
      targetRowIndex: firstIndex,
      targetRowCount: targetRows.length,
      belowFold,
      visibleRows: Math.floor((scroller?.clientHeight ?? 700) / 22),
    };
  }, target.id);

  return {
    systemLatencyS: observed.latencyMs / 1000,
    rowsPresented: observed.rowsPresented,
    targetRowIndex: observed.targetRowIndex,
    targetRowCount: observed.targetRowCount,
    // Scrolling to bring the row into view is one extra pointer operation.
    uiOperations: observed.belowFold ? 1 : 0,
    visibleRows: observed.visibleRows,
    alertsInjected: injected.alerts,
  };
}

async function main() {
  const rng = makeRng(CONFIG.seed);
  const hosts = await loadHosts();
  console.log(`MTTI harness: ${CONFIG.trials} trials, ${CONFIG.noise} noise alerts/trial, ` +
              `${hosts.length} candidate hosts, seed ${CONFIG.seed}`);

  const browser = await chromium.launch({
    channel: "chrome",
    headless: !CONFIG.headed,
    args: ["--enable-unsafe-swiftshader", "--use-gl=angle", "--enable-webgl"],
  });

  const mapPage = await openApp(browser, "map");
  const logPage = await openApp(browser, "log");

  const trials = [];
  for (let i = 0; i < CONFIG.trials; i += 1) {
    const target = hosts[Math.floor(rng() * hosts.length)];
    const scenario = CONFIG.scenarios[Math.floor(rng() * CONFIG.scenarios.length)];

    // Both conditions see the same incident on the same host with the same
    // background noise. Order is alternated so any drift in the running system
    // cannot systematically favour one condition.
    await clearAlerts();
    await seedNoise(CONFIG.noise);
    await new Promise((r) => setTimeout(r, 350));

    let map; let log;
    if (i % 2 === 0) {
      map = await measureMapTrial(mapPage, target, scenario);
      await clearAlerts();
      await seedNoise(CONFIG.noise);
      await new Promise((r) => setTimeout(r, 350));
      log = await measureLogTrial(logPage, target, scenario);
    } else {
      log = await measureLogTrial(logPage, target, scenario);
      await clearAlerts();
      await seedNoise(CONFIG.noise);
      await new Promise((r) => setTimeout(r, 350));
      map = await measureMapTrial(mapPage, target, scenario);
    }

    const mapModel = modelMapCondition(map);
    const logModel = modelLogCondition(log);
    trials.push({
      index: i, target: target.id, targetName: target.name, scenario,
      map, log,
      mapPredictedS: mapModel.total, logPredictedS: logModel.total,
      mapBreakdown: mapModel.breakdown, logBreakdown: logModel.breakdown,
      improvement: (logModel.total - mapModel.total) / logModel.total,
    });

    process.stdout.write(
      `  trial ${String(i + 1).padStart(2)}/${CONFIG.trials} ${scenario.padEnd(17)} ` +
      `map ${mapModel.total.toFixed(2)}s (lat ${(map.systemLatencyS * 1000).toFixed(0)}ms, ` +
      `${map.competingMarkedNodes} competing, banner ${map.identityPresentedWithoutInteraction ? "hit" : "miss"})  ` +
      `log ${logModel.total.toFixed(2)}s (lat ${(log.systemLatencyS * 1000).toFixed(0)}ms, ` +
      `row ${log.targetRowIndex}/${log.rowsPresented}, ${log.targetRowCount} rows)  ` +
      `-> ${(trials[i].improvement * 100).toFixed(1)}%\n`);
  }

  await browser.close();

  const mapTimes = trials.map((t) => t.mapPredictedS);
  const logTimes = trials.map((t) => t.logPredictedS);
  const mapLatency = trials.map((t) => t.map.systemLatencyS * 1000);
  const logLatency = trials.map((t) => t.log.systemLatencyS * 1000);
  const improvementMedian = (median(logTimes) - median(mapTimes)) / median(logTimes);
  const ci = bootstrapImprovementCI(logTimes, mapTimes);
  const sens = sensitivity(trials);

  const report = {
    generated_at: new Date().toISOString(),
    config: CONFIG,
    measured: {
      note: "Directly observed from the running interfaces. No modelling.",
      map_system_latency_ms: {
        median: median(mapLatency), mean: mean(mapLatency),
        p95: [...mapLatency].sort((a, b) => a - b)[Math.floor(mapLatency.length * 0.95)],
        max: Math.max(...mapLatency),
      },
      log_system_latency_ms: {
        median: median(logLatency), mean: mean(logLatency),
        p95: [...logLatency].sort((a, b) => a - b)[Math.floor(logLatency.length * 0.95)],
        max: Math.max(...logLatency),
      },
      map_competing_marked_nodes: { median: median(trials.map((t) => t.map.competingMarkedNodes)) },
      log_rows_presented: { median: median(trials.map((t) => t.log.rowsPresented)) },
      log_target_row_index: { median: median(trials.map((t) => t.log.targetRowIndex)) },
      log_rows_per_target: { median: median(trials.map((t) => t.log.targetRowCount)) },
      map_fps: { median: median(trials.map((t) => t.map.fps).filter(Boolean)) },
      map_banner_named_target_fraction:
        trials.filter((t) => t.map.identityPresentedWithoutInteraction).length / trials.length,
    },
    modelled: {
      note: "KLM-GOMS + visual-search model applied to the measurements above. "
          + "Not a human measurement; see MTTI.md for the human protocol.",
      map_mtti_s: { median: median(mapTimes), mean: mean(mapTimes), sd: stdev(mapTimes) },
      log_mtti_s: { median: median(logTimes), mean: mean(logTimes), sd: stdev(logTimes) },
      improvement_median: improvementMedian,
      improvement_mean: mean(trials.map((t) => t.improvement)),
      improvement_ci95: ci,
      target: 0.40,
      meets_target: improvementMedian >= 0.40,
    },
    sensitivity: {
      note: "The comparison re-run across plausible parameter ranges.",
      min: sens.min, max: sens.max, median: sens.median,
      fraction_of_parameter_grid_above_40pct: sens.fractionAbove40,
      grid_size: sens.rows.length,
    },
    trials,
  };

  const outPath = path.resolve(CONFIG.out);
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, JSON.stringify(report, null, 2));

  console.log("\n" + "=".repeat(74));
  console.log("  MTTI experiment — model-based, no human participants");
  console.log("=".repeat(74));
  console.log("  MEASURED (real observations of the two interfaces)");
  console.log(`    3D map   alert -> on screen : median ${report.measured.map_system_latency_ms.median.toFixed(0)} ms`
            + ` (p95 ${report.measured.map_system_latency_ms.p95.toFixed(0)} ms)`);
  console.log(`    log view alert -> on screen : median ${report.measured.log_system_latency_ms.median.toFixed(0)} ms`
            + ` (p95 ${report.measured.log_system_latency_ms.p95.toFixed(0)} ms)`);
  console.log(`    log rows presented          : median ${report.measured.log_rows_presented.median}`);
  console.log(`    log rows for the target host: median ${report.measured.log_rows_per_target.median}`);
  console.log(`    map nodes competing visually: median ${report.measured.map_competing_marked_nodes.median}`);
  console.log(`    map frame rate              : median ${report.measured.map_fps.median.toFixed(0)} fps`);
  console.log("\n  MODELLED (KLM-GOMS + visual search applied to the above)");
  console.log(`    log view MTTI  : median ${median(logTimes).toFixed(2)} s  (mean ${mean(logTimes).toFixed(2)})`);
  console.log(`    3D map MTTI    : median ${median(mapTimes).toFixed(2)} s  (mean ${mean(mapTimes).toFixed(2)})`);
  console.log(`    improvement    : ${(improvementMedian * 100).toFixed(1)}%  `
            + `(95% CI ${(ci.lower * 100).toFixed(1)}% .. ${(ci.upper * 100).toFixed(1)}%)`);
  console.log(`    target 40%     : ${improvementMedian >= 0.40 ? "MET" : "NOT MET"}`);
  console.log("\n  SENSITIVITY (same comparison across plausible parameters)");
  console.log(`    improvement range: ${(sens.min * 100).toFixed(1)}% .. ${(sens.max * 100).toFixed(1)}%`
            + `  (median ${(sens.median * 100).toFixed(1)}%)`);
  console.log(`    parameter settings meeting 40%: `
            + `${(sens.fractionAbove40 * 100).toFixed(0)}% of ${sens.rows.length}`);
  console.log("=".repeat(74));
  console.log(`  report: ${outPath}`);
  console.log("  This is a model, not a human study. See MTTI.md.\n");

  return report;
}

main().catch((err) => { console.error(err); process.exit(1); });
