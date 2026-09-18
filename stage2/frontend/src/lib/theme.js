/**
 * Design tokens and the severity visual language.
 *
 * ## The one rule
 *
 * Severity is never encoded by colour alone. Every level also differs in
 * **size**, **halo**, **pulse rate**, **glyph** and **text label**. That
 * redundancy is what keeps the map readable for the ~8% of men with a colour
 * vision deficiency, in greyscale, on a washed-out projector, and from across a
 * room — and it is also what lets the colourblind palette be a pure luminance
 * ramp without losing information.
 *
 * ## Palettes
 *
 * - `default`      "deep space": a near-black blue base with a cyan accent, and
 *                  a severity ramp derived from Okabe-Ito so it is ordered by
 *                  both hue and luminance.
 * - `colorblind`   a monotone luminance ramp (deep blue → cyan → amber → white).
 *                  Unambiguous under protanopia, deuteranopia and tritanopia
 *                  because the ordering is carried by lightness.
 * - `highContrast` maximum luminance separation on pure black, for low-vision
 *                  users and bright rooms.
 *
 * ## Surfaces
 *
 * Panels are not one flat colour. Four elevation levels, each a little lighter
 * and a little bluer than the last, plus a hairline top-edge highlight, give the
 * chrome depth without shadows heavy enough to muddy a dark UI.
 */

export const SEVERITIES = ["none", "info", "low", "medium", "high", "critical"];
export const SEVERITY_RANK = { none: 0, info: 1, low: 2, medium: 3, high: 4, critical: 5 };

/** Shared spacing/typographic rhythm. Everything in the CSS derives from these. */
export const SCALE = {
  space: [0, 2, 4, 6, 8, 12, 16, 20, 24, 32, 40, 56],
  radius: { xs: 3, sm: 5, md: 8, lg: 12, xl: 18, pill: 999 },
  text: { xs: 10, sm: 11, base: 12, md: 13, lg: 15, xl: 18, xxl: 24, display: 30 },
};

export const PALETTES = {
  default: {
    label: "Deep space",
    // --- scene ---------------------------------------------------------
    background: "#05070d",
    fog: "#05070d",
    horizon: "#0b1220",
    grid: "#111c2e",
    gridAccent: "#16263d",
    link: "#1d2b40",
    linkActive: "#38bdf8",
    linkFlow: "#7dd3fc",
    // --- chrome --------------------------------------------------------
    surface0: "#05070d",
    surface1: "#0a101b",
    surface2: "#101927",
    surface3: "#162133",
    border: "#1c2940",
    borderStrong: "#2a3d5c",
    edgeHighlight: "rgba(255,255,255,0.055)",
    text: "#e8f0fb",
    textDim: "#8095b0",
    textFaint: "#556b8a",
    accent: "#22d3ee",
    accentDim: "#0e7490",
    accentGlow: "rgba(34,211,238,0.35)",
    ok: "#34d399",
    // Text colour to place *on* a critical fill. Stated per palette because
    // "critical" is hot pink in one and pure white in the others, and white on
    // white is how an accessible palette quietly becomes unreadable.
    criticalInk: "#ffffff",
    // --- severity ------------------------------------------------------
    severity: {
      none: "#3c4a63",
      info: "#56b4e9",
      low: "#f0e442",
      medium: "#f59e0b",
      high: "#f2622e",
      critical: "#ff2d6f",
    },
    deviceType: {
      router: "#a78bfa", firewall: "#fb7185", switch: "#60a5fa",
      server: "#34d399", workstation: "#94a3b8", printer: "#fbbf24",
      iot: "#22d3ee", wireless_ap: "#f472b6", unknown: "#64748b",
    },
    bloom: 0.72,
  },

  colorblind: {
    label: "Colourblind safe",
    background: "#05070c",
    fog: "#05070c",
    horizon: "#0a1019",
    grid: "#111a26",
    gridAccent: "#182535",
    link: "#1d2937",
    linkActive: "#9ad1ff",
    linkFlow: "#cbe7ff",
    surface0: "#05070c",
    surface1: "#0a0f18",
    surface2: "#101825",
    surface3: "#16202f",
    border: "#1d2837",
    borderStrong: "#2b3b50",
    edgeHighlight: "rgba(255,255,255,0.05)",
    text: "#eef4fa",
    textDim: "#8ea2b8",
    textFaint: "#5c7089",
    accent: "#9ad1ff",
    accentDim: "#2166ac",
    accentGlow: "rgba(154,209,255,0.32)",
    ok: "#92c5de",
    criticalInk: "#05070c",
    // Ordered by luminance, so the ranking survives any colour vision type.
    severity: {
      none: "#39445a",
      info: "#2166ac",
      low: "#4393c3",
      medium: "#92c5de",
      high: "#f4d166",
      critical: "#ffffff",
    },
    deviceType: {
      router: "#8da0cb", firewall: "#fc8d62", switch: "#66c2a5",
      server: "#a6d854", workstation: "#dfe6ee", printer: "#ffd92f",
      iot: "#8dd3c7", wireless_ap: "#bebada", unknown: "#8a97a6",
    },
    bloom: 0.62,
  },

  highContrast: {
    label: "High contrast",
    background: "#000000",
    fog: "#000000",
    horizon: "#000000",
    grid: "#2a2a2a",
    gridAccent: "#3a3a3a",
    link: "#5a5a5a",
    linkActive: "#ffffff",
    linkFlow: "#ffffff",
    surface0: "#000000",
    surface1: "#000000",
    surface2: "#0a0a0a",
    surface3: "#141414",
    border: "#ffffff",
    borderStrong: "#ffffff",
    edgeHighlight: "rgba(255,255,255,0.18)",
    text: "#ffffff",
    textDim: "#e6e6e6",
    textFaint: "#bdbdbd",
    accent: "#ffff00",
    accentDim: "#8a8a00",
    accentGlow: "rgba(255,255,0,0.4)",
    ok: "#00ff9c",
    criticalInk: "#000000",
    severity: {
      none: "#7a7a7a",
      info: "#00b3ff",
      low: "#00ffcc",
      medium: "#ffff00",
      high: "#ff9500",
      critical: "#ffffff",
    },
    deviceType: {
      router: "#ffffff", firewall: "#ff6666", switch: "#66b3ff",
      server: "#66ff66", workstation: "#d9d9d9", printer: "#ffcc00",
      iot: "#00ffff", wireless_ap: "#ff66ff", unknown: "#a6a6a6",
    },
    bloom: 0.25,
  },
};

/**
 * Redundant (non-colour) encoding of severity.
 *   scale     node radius multiplier
 *   halo      radius of the surrounding ring, 0 for none
 *   pulse     pulses per second, 0 for static
 *   emissive  emissive strength, which is what the bloom pass picks up
 *   glyph     text marker for lists and the accessibility layer
 */
export const SEVERITY_STYLE = {
  none: { scale: 1.0, halo: 0, pulse: 0, emissive: 0.05, glyph: "·", label: "normal" },
  info: { scale: 1.15, halo: 0, pulse: 0, emissive: 0.35, glyph: "i", label: "info" },
  low: { scale: 1.35, halo: 1.9, pulse: 0.5, emissive: 0.55, glyph: "!", label: "low" },
  medium: { scale: 1.7, halo: 2.4, pulse: 0.9, emissive: 0.85, glyph: "!!", label: "medium" },
  high: { scale: 2.1, halo: 3.0, pulse: 1.5, emissive: 1.25, glyph: "!!!", label: "high" },
  critical: { scale: 2.6, halo: 3.8, pulse: 2.4, emissive: 1.7, glyph: "!!!!", label: "critical" },
};

export const DEVICE_GLYPH = {
  router: "◆", firewall: "▲", switch: "■", server: "▮",
  workstation: "●", printer: "▬", iot: "◇", wireless_ap: "☰", unknown: "○",
};

/** Single-path SVG icons, inlined so there is no icon-font dependency. */
export const DEVICE_ICON = {
  router: "M12 2 3 7v10l9 5 9-5V7zM12 7v10M7.5 9.5l9 5M16.5 9.5l-9 5",
  firewall: "M3 6h18M3 12h18M3 18h18M8 6v6M16 6v6M12 12v6",
  switch: "M3 8h18v8H3zM7 12h.01M11 12h.01M15 12h.01",
  server: "M4 4h16v6H4zM4 14h16v6H4zM7 7h.01M7 17h.01",
  workstation: "M4 5h16v10H4zM9 19h6M12 15v4",
  printer: "M7 9V4h10v5M7 15h10v5H7zM4 9h16v6H4z",
  iot: "M12 20v-6M8 8a4 4 0 0 1 8 0M5 6a7 7 0 0 1 14 0M12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4",
  wireless_ap: "M12 20v-4M6 12a6 6 0 0 1 12 0M3 9a9 9 0 0 1 18 0M12 16a2 2 0 1 0 0-4 2 2 0 0 0 0 4",
  unknown: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18M12 16h.01M9.5 9a2.5 2.5 0 1 1 3 2.5V13",
};

export function getPalette(name) {
  return PALETTES[name] || PALETTES.default;
}

/** "#rrggbb" -> 0xrrggbb, for three.js Color. */
export function hexToInt(hex) {
  return parseInt(hex.replace("#", ""), 16);
}

export function severityColor(palette, severity) {
  return palette.severity[severity] || palette.severity.none;
}

export function isAlerting(severity) {
  return Boolean(severity) && severity !== "none";
}

export function rgba(hex, alpha) {
  const value = parseInt(hex.replace("#", ""), 16);
  return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, ${alpha})`;
}

/** Write the palette onto :root as custom properties. */
export function applyPaletteToCss(palette) {
  const root = document.documentElement;
  const set = (name, value) => root.style.setProperty(name, value);

  set("--bg", palette.background);
  set("--horizon", palette.horizon);
  set("--surface-0", palette.surface0);
  set("--surface-1", palette.surface1);
  set("--surface-2", palette.surface2);
  set("--surface-3", palette.surface3);
  set("--border", palette.border);
  set("--border-strong", palette.borderStrong);
  set("--edge-highlight", palette.edgeHighlight);
  set("--text", palette.text);
  set("--text-dim", palette.textDim);
  set("--text-faint", palette.textFaint);
  set("--accent", palette.accent);
  set("--accent-dim", palette.accentDim);
  set("--accent-glow", palette.accentGlow);
  set("--ok", palette.ok);
  set("--critical-ink", palette.criticalInk || "#ffffff");
  // Legacy aliases kept so older rules and tests keep resolving.
  set("--panel", palette.surface1);
  set("--panel-border", palette.border);

  for (const severity of SEVERITIES) {
    const hex = palette.severity[severity];
    set(`--sev-${severity}`, hex);
    set(`--sev-${severity}-glow`, rgba(hex, 0.45));
    set(`--sev-${severity}-wash`, rgba(hex, 0.12));
  }
  for (const [type, hex] of Object.entries(palette.deviceType)) {
    set(`--dev-${type}`, hex);
  }
}
