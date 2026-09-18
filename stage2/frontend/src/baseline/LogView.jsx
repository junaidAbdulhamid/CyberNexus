import React, { useEffect, useMemo, useState } from "react";

/**
 * The baseline interface the 3D map is measured against.
 *
 * This is written to be a *fair* comparison, not a strawman. It is a competent
 * SOC log console: reverse-chronological, auto-refreshing, sortable, with
 * severity and score shown as text, a working filter box, and the same node
 * detail one click away. Everything the 3D view knows is reachable here.
 *
 * The one thing it cannot do is show the shape of the network, and that is
 * precisely the variable the MTTI experiment is testing. If this view were
 * deliberately bad, the resulting improvement number would be meaningless.
 */
export default function LogView({ api, onSelect, selectedId }) {
  const [alerts, setAlerts] = useState([]);
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState({ key: "detected_at", dir: "desc" });
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [lastRefresh, setLastRefresh] = useState(0);

  const refresh = async () => {
    try {
      const data = await api.alerts({ limit: 500, minSeverity: "info" });
      setAlerts(data.alerts || []);
      setLastRefresh(Date.now());
    } catch {
      /* keep the last good page rather than blanking the console */
    }
  };

  useEffect(() => {
    refresh();
    if (!autoRefresh) return;
    const id = setInterval(refresh, 1000);
    return () => clearInterval(id);
  }, [api, autoRefresh]);

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    const filtered = q
      ? alerts.filter((a) =>
          `${a.src_ip} ${a.dst_ip} ${a.node_id} ${a.severity} ${a.dst_port}`.toLowerCase().includes(q))
      : alerts;
    const dir = sort.dir === "asc" ? 1 : -1;
    const rank = { info: 1, low: 2, medium: 3, high: 4, critical: 5 };
    return [...filtered].sort((a, b) => {
      const av = sort.key === "severity" ? rank[a.severity] : a[sort.key];
      const bv = sort.key === "severity" ? rank[b.severity] : b[sort.key];
      return av > bv ? dir : av < bv ? -dir : 0;
    });
  }, [alerts, query, sort]);

  const header = (key, label) => (
    <th scope="col">
      <button
        type="button" className="th-sort"
        onClick={() => setSort((s) => ({ key, dir: s.key === key && s.dir === "desc" ? "asc" : "desc" }))}
        aria-sort={sort.key === key ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
      >
        {label}{sort.key === key ? (sort.dir === "asc" ? " ▲" : " ▼") : ""}
      </button>
    </th>
  );

  return (
    <div className="logview" data-testid="log-view">
      <div className="logview-toolbar">
        <label className="field-label" htmlFor="log-filter">Filter</label>
        <input
          id="log-filter" type="search" className="search-input"
          placeholder="IP, node, port, severity…"
          value={query} onChange={(e) => setQuery(e.target.value)}
        />
        <label className="checkbox">
          <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />
          Auto-refresh (1 s)
        </label>
        <button type="button" className="btn btn-small" onClick={refresh}>Refresh now</button>
        <span className="dim">
          {rows.length} alerts · updated {lastRefresh ? new Date(lastRefresh).toLocaleTimeString() : "—"}
        </span>
      </div>

      <div className="logview-scroll">
        <table className="log-table">
          <caption className="sr-only">Alert log, most recent first</caption>
          <thead>
            <tr>
              {header("detected_at", "Time")}
              {header("severity", "Severity")}
              {header("score", "Score")}
              {header("src_ip", "Source")}
              {header("dst_ip", "Destination")}
              {header("dst_port", "Port")}
              <th scope="col">Host</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((alert) => (
              <tr
                key={alert.flow_id}
                className={`sev-${alert.severity} ${selectedId === alert.node_id ? "is-selected" : ""}`}
                data-testid={`log-row-${alert.node_id}`}
                data-severity={alert.severity}
              >
                <td className="mono">{new Date(alert.detected_at * 1000).toLocaleTimeString()}</td>
                <td><span className={`sev-text sev-${alert.severity}`}>{alert.severity}</span></td>
                <td>{alert.score.toFixed(3)}</td>
                <td className="mono">{alert.src_ip}:{alert.src_port}</td>
                <td className="mono">{alert.dst_ip}</td>
                <td>{alert.dst_port}</td>
                <td>
                  {alert.node_id ? (
                    <button
                      type="button" className="link-button"
                      onClick={() => onSelect(alert.node_id)}
                      data-testid={`log-node-${alert.node_id}`}
                    >
                      {alert.node_id}
                    </button>
                  ) : <span className="dim">unmapped</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <p className="empty">No alerts.</p>}
      </div>
    </div>
  );
}
