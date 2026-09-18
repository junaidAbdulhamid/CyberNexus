import React from "react";
import { SEVERITY_STYLE } from "../lib/theme.js";

/**
 * The non-visual path to the same information.
 *
 * A 3D map is the least accessible way to present anything, so the visualisation
 * is never the only channel:
 *
 *  - a live region announces new alerts to a screen reader, `assertive` for
 *    critical and `polite` for everything else, so an analyst using a screen
 *    reader learns about an incident at the same moment a sighted one sees the
 *    node flash;
 *  - a visually hidden but fully focusable list mirrors the alerting nodes, so
 *    Tab order reaches every host the map is highlighting;
 *  - all of it works with the canvas off-screen or the GPU unavailable.
 */
export function LiveRegion({ announcements }) {
  const latest = announcements[0];
  const assertive = latest?.severity === "critical" || latest?.severity === "high";
  return (
    <>
      <div
        className="sr-only" role="status"
        aria-live={assertive ? "off" : "polite"} aria-atomic="true"
        data-testid="live-region-polite"
      >
        {!assertive && latest ? latest.message : ""}
      </div>
      <div
        className="sr-only" role="alert"
        aria-live={assertive ? "assertive" : "off"} aria-atomic="true"
        data-testid="live-region-assertive"
      >
        {assertive && latest ? latest.message : ""}
      </div>
    </>
  );
}

export function KeyboardNodeList({ states, topology, onSelect, selectedId }) {
  const ranked = states.ranked();
  return (
    <ul className="sr-only" aria-label="Alerting hosts, worst first">
      {ranked.map((state) => {
        const index = topology?.index.get(state.node_id);
        const node = index !== undefined ? topology.nodes[index] : null;
        return (
          <li key={state.node_id}>
            <button
              type="button"
              onClick={() => onSelect(state.node_id)}
              aria-current={selectedId === state.node_id ? "true" : undefined}
            >
              {SEVERITY_STYLE[state.severity].label} severity on {node?.name || state.node_id},
              address {node?.ip || "unknown"}, {state.alert_count} alerts,
              latest port {state.top_ports?.[0] ?? "unknown"}
            </button>
          </li>
        );
      })}
    </ul>
  );
}

export function KeyboardHelp({ open, onClose }) {
  if (!open) return null;
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal" role="dialog" aria-modal="true" aria-labelledby="kb-help-title"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="kb-help-title">Keyboard shortcuts</h2>
        <dl className="shortcut-list">
          <dt><kbd>⌘K</kbd></dt><dd>Command palette: search hosts, run actions</dd>
          <dt><kbd>/</kbd></dt><dd>Focus search</dd>
          <dt><kbd>n</kbd> / <kbd>↓</kbd></dt><dd>Next alerting host</dd>
          <dt><kbd>p</kbd> / <kbd>↑</kbd></dt><dd>Previous alerting host</dd>
          <dt><kbd>Enter</kbd></dt><dd>Fly camera to the selected host</dd>
          <dt><kbd>w</kbd></dt><dd>Jump to the worst active alert</dd>
          <dt><kbd>a</kbd></dt><dd>Acknowledge the selected host</dd>
          <dt><kbd>f</kbd></dt><dd>Frame the whole network</dd>
          <dt><kbd>c</kbd></dt><dd>Cycle colour palette</dd>
          <dt><kbd>l</kbd></dt><dd>Toggle level-of-detail clustering</dd>
          <dt><kbd>b</kbd></dt><dd>Switch between map and log view</dd>
          <dt><kbd>Esc</kbd></dt><dd>Close panel or dialog</dd>
          <dt><kbd>?</kbd></dt><dd>This help</dd>
        </dl>
        <button type="button" className="btn btn-primary" onClick={onClose} autoFocus>Close</button>
      </div>
    </div>
  );
}
