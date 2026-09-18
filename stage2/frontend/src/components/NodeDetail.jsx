import React, { useEffect, useState } from "react";
import { DEVICE_GLYPH, SEVERITY_STYLE } from "../lib/theme.js";

/**
 * Detail panel for the selected host.
 *
 * Answers, in this order, the questions an analyst asks on seeing a marked
 * node: what is it, how bad, what did it do, and what do I do now. Identity and
 * severity come first because they decide whether the rest matters.
 *
 * Note what is absent: packet contents. The panel shows five-tuple metadata and
 * scores only, because that is all Stage 1 emits and all Stage 2 stores.
 */
export default function NodeDetail({ api, nodeId, onClose, onTicket, onAcknowledge, onFocus }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    if (!nodeId) { setDetail(null); return; }
    setError(null);
    api.node(nodeId)
      .then((data) => { if (!cancelled) setDetail(data); })
      .catch((err) => { if (!cancelled) setError(err.message); });
    return () => { cancelled = true; };
  }, [api, nodeId]);

  if (!nodeId) return null;

  if (error) {
    return (
      <section className="detail-panel" role="complementary" aria-label="Node detail">
        <header className="detail-header">
          <h2>Node detail</h2>
          <button type="button" className="btn-icon" onClick={onClose} aria-label="Close detail panel">✕</button>
        </header>
        <p className="error">Could not load node: {error}</p>
      </section>
    );
  }

  if (!detail) {
    return (
      <section className="detail-panel" role="complementary" aria-label="Node detail" aria-busy="true">
        <p className="empty">Loading…</p>
      </section>
    );
  }

  const { node, state, alerts, neighbours, telemetry } = detail;
  const severity = state?.severity || "none";
  const style = SEVERITY_STYLE[severity];

  const act = async (fn) => {
    setBusy(true);
    try { await fn(); } finally { setBusy(false); }
  };

  return (
    <section
      className={`detail-panel sev-border-${severity}`}
      role="complementary"
      aria-label={`Details for ${node.name}`}
      data-testid="node-detail"
      data-node-id={node.id}
      data-severity={severity}
    >
      <header className="detail-header">
        <h2>
          <span aria-hidden="true">{DEVICE_GLYPH[node.device_type] || "○"}</span> {node.name}
        </h2>
        <button type="button" className="btn-icon" onClick={onClose} aria-label="Close detail panel">✕</button>
      </header>

      <div className={`severity-banner sev-${severity}`}>
        <span className="sev-glyph" aria-hidden="true">{style.glyph}</span>
        <span>
          <strong>{style.label.toUpperCase()}</strong>
          {state ? ` · score ${state.score.toFixed(2)} · ${state.alert_count} alert${state.alert_count === 1 ? "" : "s"}` : " · no active alerts"}
        </span>
      </div>

      <dl className="detail-grid">
        <dt>IP</dt><dd>{node.ip || "—"}</dd>
        <dt>MAC</dt><dd className="mono">{node.mac || "—"}</dd>
        <dt>Vendor</dt><dd>{node.vendor}</dd>
        <dt>Type</dt><dd>{node.device_type}</dd>
        <dt>OS</dt><dd>{node.os || "—"}</dd>
        <dt>Subnet</dt><dd>{node.subnet}</dd>
        <dt>Site</dt><dd>{node.site}</dd>
        <dt>Criticality</dt><dd>{"★".repeat(node.criticality || 0) || "—"}</dd>
        <dt>Discovered by</dt><dd>{(node.discovered_by || []).join(", ") || "—"}</dd>
        <dt>Services</dt><dd>{(telemetry.services || []).join(", ") || "—"}</dd>
        <dt>Neighbours</dt><dd>{neighbours.length}</dd>
      </dl>

      {node.tags?.length > 0 && (
        <div className="tag-row">
          {node.tags.map((tag) => <span key={tag} className="tag">{tag}</span>)}
        </div>
      )}

      <div className="detail-actions">
        <button type="button" className="btn" onClick={() => onFocus(node.id)}>
          Focus camera
        </button>
        <button
          type="button" className="btn" disabled={!state || busy}
          onClick={() => act(() => onAcknowledge(node.id))}
        >
          Acknowledge
        </button>
        <button
          type="button" className="btn btn-primary" disabled={!state || busy}
          onClick={() => act(() => onTicket(node.id))}
          data-testid="create-ticket"
        >
          Create ticket
        </button>
      </div>

      <h3 className="panel-title">
        Recent alerts <span className="count-badge">{alerts.length}</span>
      </h3>
      {alerts.length === 0 ? (
        <p className="empty">No alerts recorded for this host.</p>
      ) : (
        <table className="alert-table">
          <caption className="sr-only">Recent alerts for {node.name}</caption>
          <thead>
            <tr><th scope="col">Time</th><th scope="col">Peer</th><th scope="col">Port</th><th scope="col">Score</th></tr>
          </thead>
          <tbody>
            {alerts.slice(0, 15).map((alert) => (
              <tr key={alert.flow_id} className={`sev-${alert.severity}`}>
                <td>{new Date(alert.detected_at * 1000).toLocaleTimeString()}</td>
                <td className="mono">{alert.src_ip === node.ip ? alert.dst_ip : alert.src_ip}</td>
                <td>{alert.dst_port}</td>
                <td>{alert.score.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
