import React, { useEffect, useState } from "react";
import { SEVERITY_STYLE } from "../lib/theme.js";
import Icon, { DeviceIcon } from "./Icon.jsx";

/**
 * Detail panel for the selected host.
 *
 * Ordered to answer, in the order an analyst asks them: what is it, how bad is
 * it, what did it do, what do I do now. Identity and severity come first
 * because they decide whether the rest matters.
 *
 * Note what is absent: packet contents. The panel shows five-tuple metadata and
 * scores, because that is all Stage 1 emits and all Stage 2 stores.
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
      <section className="detail-panel" role="complementary" aria-label="Host detail">
        <header className="detail-header">
          <h2>Host detail</h2>
          <button type="button" className="btn-icon" onClick={onClose} aria-label="Close detail panel">
            <Icon name="x" size={15} />
          </button>
        </header>
        <div className="detail-body"><p className="error">Could not load host: {error}</p></div>
      </section>
    );
  }

  if (!detail) {
    return (
      <section className="detail-panel" role="complementary" aria-label="Host detail" aria-busy="true">
        <div className="detail-body"><p className="empty">Loading…</p></div>
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
        <div style={{ minWidth: 0 }}>
          <h2>
            <DeviceIcon type={node.device_type} size={16} />
            <span className="detail-title-text">{node.name}</span>
          </h2>
          <div className="detail-subtitle">{node.ip} · {node.device_type}</div>
        </div>
        <button type="button" className="btn-icon" onClick={onClose} aria-label="Close detail panel">
          <Icon name="x" size={15} />
        </button>
      </header>

      <div className="detail-body">
        <div className={`severity-banner sev-${severity}`}>
          <span className={`sev-glyph sev-${severity}`} aria-hidden="true">{style.glyph}</span>
          <span>
            <strong>{style.label.toUpperCase()}</strong>
            {state
              ? ` · peak ${state.score.toFixed(2)} · ${state.alert_count} alert${state.alert_count === 1 ? "" : "s"}`
              : " · no active alerts"}
          </span>
        </div>

        <dl className="detail-grid">
          <dt>Address</dt><dd className="mono">{node.ip || "—"}</dd>
          <dt>Hardware</dt><dd className="mono">{node.mac || "—"}</dd>
          <dt>Vendor</dt><dd>{node.vendor}</dd>
          <dt>Operating&nbsp;system</dt><dd>{node.os || "—"}</dd>
          <dt>Subnet</dt><dd className="mono">{node.subnet}</dd>
          <dt>Site</dt><dd>{node.site}</dd>
          <dt>Criticality</dt>
          <dd>{node.criticality ? "★".repeat(node.criticality) + "☆".repeat(3 - node.criticality) : "—"}</dd>
          <dt>Discovered&nbsp;by</dt><dd>{(node.discovered_by || []).join(", ") || "—"}</dd>
          <dt>Services</dt><dd>{(telemetry.services || []).join(", ") || "—"}</dd>
          <dt>Neighbours</dt><dd>{neighbours.length}</dd>
          {state?.top_ports?.length > 0 && (
            <>
              <dt>Target ports</dt>
              <dd className="mono">{state.top_ports.join(", ")}</dd>
            </>
          )}
        </dl>

        {node.tags?.length > 0 && (
          <div className="tag-row">
            {node.tags.map((tag) => <span key={tag} className="tag">{tag}</span>)}
          </div>
        )}

        <div className="detail-actions">
          <button type="button" className="btn" onClick={() => onFocus(node.id)}>
            <Icon name="target" size={12} /> Focus
          </button>
          <button
            type="button" className="btn" disabled={!state || busy}
            onClick={() => act(() => onAcknowledge(node.id))}
          >
            <Icon name="check" size={12} /> Acknowledge
          </button>
          <button
            type="button" className="btn btn-primary" disabled={!state || busy}
            onClick={() => act(() => onTicket(node.id))}
            data-testid="create-ticket"
          >
            <Icon name="ticket" size={12} /> Create ticket
          </button>
        </div>

        <h3 className="panel-title">
          <Icon name="clock" size={11} />
          Recent alerts
          <span className="count-badge">{alerts.length}</span>
        </h3>
        {alerts.length === 0 ? (
          <p className="empty">No alerts recorded for this host.</p>
        ) : (
          <table className="alert-table">
            <caption className="sr-only">Recent alerts for {node.name}</caption>
            <thead>
              <tr>
                <th scope="col">Time</th>
                <th scope="col">Peer</th>
                <th scope="col">Port</th>
                <th scope="col">Score</th>
              </tr>
            </thead>
            <tbody>
              {alerts.slice(0, 15).map((alert) => (
                <tr key={alert.flow_id} className={`sev-${alert.severity}`}>
                  <td>{new Date(alert.detected_at * 1000).toLocaleTimeString()}</td>
                  <td>{alert.src_ip === node.ip ? alert.dst_ip : alert.src_ip}</td>
                  <td>{alert.dst_port}</td>
                  <td>
                    <span className="score-cell">
                      <span className="score-bar">
                        <span style={{
                          width: `${Math.round(alert.score * 100)}%`,
                          background: `var(--sev-${alert.severity})`,
                        }} />
                      </span>
                      {alert.score.toFixed(2)}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
