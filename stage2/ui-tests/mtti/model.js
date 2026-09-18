/**
 * The cost model that turns measured interface properties into predicted
 * identification times.
 *
 * ## Why a model at all
 *
 * Mean Time To Identify is a property of a *human* doing a task. No automated
 * harness can measure it, because an automated agent reads the DOM in
 * microseconds and has no visual system to model. Anyone who reports "our
 * automated MTTI harness measured a 40% improvement" without saying this is
 * reporting the speed of their test script.
 *
 * So the harness is split in two, and only the halves are honest:
 *
 *   **Measured** (this file does not touch): system latency from alert
 *   injection to the information being on screen; how many items the interface
 *   presents; where the target sits among them; how many pointer and keyboard
 *   operations are needed to reach a confident identification. These are real
 *   observations of the two running interfaces.
 *
 *   **Modelled** (this file): converting those observations into seconds, using
 *   two standard HCI techniques.
 *
 * ## The two techniques
 *
 * **KLM-GOMS** (Card, Moran & Newell, 1980) predicts skilled task time by
 * decomposing it into primitive operators with published average durations.
 * The operator times below are the canonical ones. Its known limits apply: it
 * models an expert doing an error-free routine task, so it under-predicts real
 * times, and it does so *in both conditions*, which is what makes the ratio
 * more trustworthy than either absolute value.
 *
 * **Visual search** (Treisman & Gelade, 1980; Wolfe, 1994 and after). A target
 * that differs from its surroundings in a single basic feature - colour, size,
 * motion - is found in roughly constant time regardless of how many distractors
 * there are ("pop-out"). A target that must be identified by conjunction or by
 * reading requires serial inspection, and time grows with the number of items.
 * This is the entire mechanism by which a map can beat a list, and it is why
 * the alerting node is drawn with a unique colour *and* a unique size *and*
 * motion: three redundant pop-out channels.
 *
 * Every parameter below is a named constant with a cited basis and can be
 * overridden from the command line. `sensitivity()` re-runs the whole
 * comparison across plausible ranges, because a single point estimate from a
 * model with assumed parameters would be false precision.
 */

/** KLM operator durations in seconds (Card, Moran & Newell 1980, Table 1). */
export const KLM = {
  K: 0.28,   // keystroke, average non-secretarial typist
  P: 1.10,   // point to a target on screen (Fitts-derived average)
  B: 0.20,   // mouse button press + release
  H: 0.40,   // home hands between keyboard and mouse
  M: 1.35,   // mental preparation / act of deciding
};

/** Visual search parameters. */
export const SEARCH = {
  // Feature ("pop-out") search: essentially flat in set size.
  popoutBase: 0.45,        // s, time to fixate a uniquely-featured target
  popoutSlope: 0.002,      // s per distractor - near zero by definition
  // Serial search through a list where items must be inspected to be judged.
  // 0.035 s/item is at the fast end of the literature and assumes the operator
  // is scanning a colour-coded severity column rather than reading each row.
  serialSlope: 0.035,      // s per item inspected
  serialFixation: 0.55,    // s to read and evaluate a candidate row
  // A self-terminating search inspects about half the list before finding a
  // present target, on average.
  selfTerminatingFraction: 0.5,
  // Cost of realising that several rows are the same host - the log view's
  // structural disadvantage: it lists events, the map shows entities.
  aggregationPerExtraRow: 0.28,
  aggregationCap: 6.0,     // s, ceiling on the aggregation penalty
};

/**
 * Predicted identification time for the 3D map condition.
 *
 * Sequence an operator performs: notice the change (pop-out), decide (M),
 * point at the marked node or its list row (P), click (B), read the identity
 * off the detail panel (M + fixation).
 */
export function modelMapCondition(measurement, params = {}) {
  const k = { ...KLM, ...(params.klm || {}) };
  const s = { ...SEARCH, ...(params.search || {}) };

  const distractors = measurement.competingMarkedNodes ?? 0;
  const detect = s.popoutBase + s.popoutSlope * distractors;

  // Several nodes alerting at once costs a ranking decision, but the list is
  // already sorted worst-first, so it is a decision, not a search.
  const triage = distractors > 0 ? k.M : 0;

  // When the interface names the host without an interaction - the incident
  // banner - the pointer operators disappear from the identification task. The
  // harness only sets this flag after verifying that the banner really did
  // display the target host's name, so it is an observation, not an assumption.
  const identityFree = measurement.identityPresentedWithoutInteraction === true;
  const point = identityFree ? 0 : k.P + k.B;

  const operators = detect + k.M + point + triage;
  const read = k.M + s.serialFixation;

  return {
    total: operators + read + measurement.systemLatencyS,
    breakdown: {
      systemLatency: measurement.systemLatencyS,
      detect,
      decide: k.M + triage,
      point,
      read,
    },
  };
}

/**
 * Predicted identification time for the log (baseline) condition.
 *
 * Sequence: notice a new row arrived, scan the list for the severe one
 * (serial), read candidate rows, realise that N rows are one host, then click
 * through to that host.
 */
export function modelLogCondition(measurement, params = {}) {
  const k = { ...KLM, ...(params.klm || {}) };
  const s = { ...SEARCH, ...(params.search || {}) };

  const rows = Math.max(measurement.rowsPresented ?? 0, 1);
  // Position of the target row is measured, not assumed; where it is unknown,
  // fall back to the average of a self-terminating search.
  const inspected = measurement.targetRowIndex != null
    ? measurement.targetRowIndex + 1
    : rows * s.selfTerminatingFraction;

  const scan = s.serialSlope * inspected + s.serialFixation;
  const extraRows = Math.max((measurement.targetRowCount ?? 1) - 1, 0);
  const aggregate = Math.min(extraRows * s.aggregationPerExtraRow, s.aggregationCap);

  // Scrolling, sorting or filtering to bring the target into view.
  const interaction = (measurement.uiOperations ?? 0) * (k.P + k.B);

  const operators = scan + k.M + aggregate + interaction + k.P + k.B;
  const read = k.M + s.serialFixation;

  return {
    total: operators + read + measurement.systemLatencyS,
    breakdown: {
      systemLatency: measurement.systemLatencyS,
      scan,
      decide: k.M,
      aggregate,
      interaction,
      point: k.P + k.B,
      read,
    },
  };
}

/** Median of a numeric array. */
export function median(values) {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

export function mean(values) {
  return values.length ? values.reduce((a, b) => a + b, 0) / values.length : 0;
}

export function stdev(values) {
  if (values.length < 2) return 0;
  const m = mean(values);
  return Math.sqrt(values.reduce((acc, v) => acc + (v - m) ** 2, 0) / (values.length - 1));
}

/** Percentile bootstrap CI for the improvement ratio between two samples. */
export function bootstrapImprovementCI(baseline, treatment, iterations = 2000, seed = 12345) {
  let state = seed >>> 0;
  const rand = () => {
    // xorshift32: deterministic, so a reported CI is reproducible.
    state ^= state << 13; state >>>= 0;
    state ^= state >> 17;
    state ^= state << 5; state >>>= 0;
    return state / 0xffffffff;
  };
  const pick = (arr) => arr[Math.floor(rand() * arr.length)];
  const ratios = [];
  for (let i = 0; i < iterations; i += 1) {
    const b = Array.from({ length: baseline.length }, () => pick(baseline));
    const t = Array.from({ length: treatment.length }, () => pick(treatment));
    const mb = median(b);
    ratios.push(mb > 0 ? (mb - median(t)) / mb : 0);
  }
  ratios.sort((a, b) => a - b);
  return {
    lower: ratios[Math.floor(iterations * 0.025)],
    upper: ratios[Math.floor(iterations * 0.975)],
  };
}

/**
 * Re-run the comparison across plausible parameter ranges.
 *
 * The point is to show whether the conclusion depends on the parameter choices.
 * If the improvement stays above the target across the whole range, the exact
 * values did not matter; if it does not, the headline number was an artefact of
 * the assumptions and should be reported as such.
 */
export function sensitivity(trials, ranges = {}) {
  const grid = {
    serialSlope: ranges.serialSlope || [0.020, 0.035, 0.060],
    popoutBase: ranges.popoutBase || [0.30, 0.45, 0.70],
    aggregationPerExtraRow: ranges.aggregationPerExtraRow || [0.0, 0.28, 0.50],
    M: ranges.M || [1.20, 1.35, 1.50],
  };
  const rows = [];
  for (const serialSlope of grid.serialSlope) {
    for (const popoutBase of grid.popoutBase) {
      for (const aggregationPerExtraRow of grid.aggregationPerExtraRow) {
        for (const M of grid.M) {
          const params = {
            klm: { M },
            search: { serialSlope, popoutBase, aggregationPerExtraRow },
          };
          const logTimes = trials.map((t) => modelLogCondition(t.log, params).total);
          const mapTimes = trials.map((t) => modelMapCondition(t.map, params).total);
          const mLog = median(logTimes);
          const mMap = median(mapTimes);
          rows.push({
            serialSlope, popoutBase, aggregationPerExtraRow, M,
            logMedian: mLog, mapMedian: mMap,
            improvement: mLog > 0 ? (mLog - mMap) / mLog : 0,
          });
        }
      }
    }
  }
  const improvements = rows.map((r) => r.improvement);
  return {
    rows,
    min: Math.min(...improvements),
    max: Math.max(...improvements),
    median: median(improvements),
    fractionAbove40: improvements.filter((v) => v >= 0.40).length / improvements.length,
  };
}
