import React from "react";
import { DEVICE_GLYPH, SEVERITY_STYLE } from "../lib/theme.js";

/**
 * The answer, on screen, without an interaction.
 *
 * This component exists because of a measurement. The first MTTI run showed the
 * map condition spending most of its modelled time on operators that had
 * nothing to do with *finding* the host: a pointer movement and a click to open
 * the detail panel, purely to learn the host's name and address. The map had
 * already solved the hard part - the node was unmissable - and then made the
 * operator work for the easy part.
 *
 * So the worst active incident names itself: host, address, what it is doing,
 * and the three actions worth taking, all reachable by keyboard. The 3D view
 * answers "where and how bad", this answers "which box", and neither requires
 * hunting.
 *
 * It is deliberately a summary of one incident, not a list. A banner that tried
 * to show every alert would be the log view with extra steps.
 */
export default function IncidentBanner({ state, node, onFocus, onAcknowledge, onTicket, onOpen }) {
  if (!state || !node) return null;
  const style = SEVERITY_STYLE[state.severity];
  const port = state.top_ports?.[0];

  return (
    <div
      className={`incident-banner sev-border-${state.severity}`}
      role="region"
      aria-label="Highest severity active incident"
      data-testid="incident-banner"
      data-node-id={state.node_id}
      data-severity={state.severity}
    >
      <span className={`incident-sev sev-${state.severity}`}>
        <span className="sev-glyph" aria-hidden="true">{style.glyph}</span>
        {style.label.toUpperCase()}
      </span>

      <button
        type="button"
        className="incident-identity"
        onClick={() => onOpen(state.node_id)}
        data-testid="incident-identity"
        title="Open host details"
      >
        <span aria-hidden="true">{DEVICE_GLYPH[node.device_type] || "○"}</span>{" "}
        <strong>{node.name}</strong>
        <span className="mono incident-ip">{node.ip}</span>
      </button>

      <span className="incident-what">
        {state.alert_count} alert{state.alert_count === 1 ? "" : "s"}
        {port ? <> · port <strong>{port}</strong></> : null}
        {node.subnet ? <> · {node.subnet}</> : null}
        {node.criticality >= 3 ? <span className="incident-crit"> · business critical</span> : null}
      </span>

      <span className="incident-actions">
        <button type="button" className="btn btn-small" onClick={() => onFocus(state.node_id)}>
          Go to host <kbd>Enter</kbd>
        </button>
        <button type="button" className="btn btn-small" onClick={() => onAcknowledge(state.node_id)}>
          Ack <kbd>a</kbd>
        </button>
        <button type="button" className="btn btn-small btn-primary" onClick={() => onTicket(state.node_id)}>
          Ticket
        </button>
      </span>
    </div>
  );
}
