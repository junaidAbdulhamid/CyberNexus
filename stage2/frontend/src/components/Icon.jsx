import React from "react";

/**
 * Inline SVG icon set.
 *
 * Inline rather than an icon font or a package: it is a few hundred bytes, it
 * inherits `currentColor` so it follows the palette for free, and it cannot
 * flash-of-unstyled-icon on load. Every icon is stroked on the same 24px grid
 * with the same 1.6 weight, which is what makes a mixed set look like one set.
 */
const PATHS = {
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16M21 21l-4.35-4.35",
  shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10",
  activity: "M22 12h-4l-3 9L9 3l-3 9H2",
  alert: "M12 9v4M12 17h.01M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0",
  network: "M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6",
  server: "M4 4h16v6H4zM4 14h16v6H4zM7 7h.01M7 17h.01",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18M12 7v5l3 2",
  check: "M20 6 9 17l-5-5",
  x: "M18 6 6 18M6 6l12 12",
  command: "M15 6a3 3 0 1 1 3 3h-3zM9 6a3 3 0 1 0-3 3h3zM9 18a3 3 0 1 1-3-3h3zM15 18a3 3 0 1 0 3-3h-3zM9 9h6v6H9z",
  zap: "M13 2 3 14h9l-1 8 10-12h-9z",
  target: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10M12 13a1 1 0 1 0 0-2 1 1 0 0 0 0 2",
  filter: "M22 3H2l8 9.46V19l4 2v-8.54z",
  download: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3",
  ticket: "M3 9V7a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v2a2 2 0 0 0 0 4v2a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-2a2 2 0 0 0 0-4M13 5v2M13 11v2M13 17v2",
  eye: "M2 12s3.64-7 10-7 10 7 10 7-3.64 7-10 7-10-7-10-7M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6",
  layers: "m12 2 9 5-9 5-9-5zM3 12l9 5 9-5M3 17l9 5 9-5",
  play: "m6 3 14 9-14 9z",
  pause: "M7 4h3v16H7zM14 4h3v16h-3z",
  live: "M12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4M7.8 7.8a6 6 0 0 0 0 8.4M16.2 16.2a6 6 0 0 0 0-8.4M4.9 4.9a10 10 0 0 0 0 14.2M19.1 19.1a10 10 0 0 0 0-14.2",
  keyboard: "M4 6h16a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1M7 10h.01M11 10h.01M15 10h.01M8 14h8",
  refresh: "M21 12a9 9 0 1 1-3-6.7M21 3v6h-6",
  chevronRight: "m9 18 6-6-6-6",
  pulse: "M3 12h4l3 8 4-16 3 8h4",
  lock: "M5 11h14v10H5zM8 11V7a4 4 0 0 1 8 0v4",
};

export default function Icon({ name, size = 14, strokeWidth = 1.6, className = "", ...rest }) {
  const d = PATHS[name];
  if (!d) return null;
  return (
    <svg
      width={size} height={size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth={strokeWidth}
      strokeLinecap="round" strokeLinejoin="round"
      className={className} aria-hidden="true" focusable="false"
      {...rest}
    >
      {/* One path element: `d` already carries every subpath, and splitting it
          apart broke any icon whose subpath began with a relative `m`. */}
      <path d={d} />
    </svg>
  );
}

/** Device-type glyph drawn from the shared icon paths in theme.js. */
export function DeviceIcon({ type, size = 13, ...rest }) {
  const name = {
    router: "network", firewall: "shield", switch: "layers", server: "server",
    workstation: "server", printer: "server", iot: "zap", wireless_ap: "live",
  }[type] || "target";
  return <Icon name={name} size={size} {...rest} />;
}
