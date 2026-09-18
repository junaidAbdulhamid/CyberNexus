import React, { useMemo } from "react";
import { DEVICE_GLYPH, SEVERITIES, SEVERITY_STYLE } from "../lib/theme.js";

/**
 * Left rail: search, filters, and the ranked alert list.
 *
 * The alert list is the keyboard-first path to the same information the 3D view
 * shows: every row is a real button, ordered worst-first, and selecting one
 * flies the camera to that node. Someone who cannot use a pointer, or who just
 * prefers a list, is never locked out of the map.
 */
export default function Sidebar({
  topology, states, filter, onFilterChange, query, onQueryChange,
  selectedId, onSelect, onAcknowledge, searchRef,
}) {
  const facets = useMemo(() => {
    const subnets = new Map();
    const vendors = new Map();
    const types = new Map();
    for (const node of topology?.nodes || []) {
      subnets.set(node.subnet, (subnets.get(node.subnet) || 0) + 1);
      vendors.set(node.vendor, (vendors.get(node.vendor) || 0) + 1);
      types.set(node.device_type, (types.get(node.device_type) || 0) + 1);
    }
    const sorted = (m) => [...m.entries()].sort((a, b) => b[1] - a[1]);
    return { subnets: sorted(subnets), vendors: sorted(vendors), types: sorted(types) };
  }, [topology]);

  const ranked = states.ranked();
  const nodeIndex = topology?.index;

  const toggle = (key, value) => {
    const next = new Set(filter[key] || []);
    next.has(value) ? next.delete(value) : next.add(value);
    onFilterChange({ [key]: next });
  };

  return (
    <aside className="sidebar" aria-label="Filters and alerts">
      <div className="panel-section">
        <label className="field-label" htmlFor="node-search">
          Search <span className="hint">(press <kbd>/</kbd>)</span>
        </label>
        <input
          id="node-search"
          ref={searchRef}
          type="search"
          className="search-input"
          placeholder="name, IP, MAC, vendor…"
          value={query}
          onChange={(e) => onQueryChange(e.target.value)}
          aria-describedby="search-help"
        />
        <p id="search-help" className="hint">
          Filters the map. Alerting hosts always stay visible.
        </p>
      </div>

      <div className="panel-section">
        <h2 className="panel-title" id="alerts-heading">
          Active alerts <span className="count-badge">{ranked.length}</span>
        </h2>
        {ranked.length === 0 ? (
          <p className="empty">No active alerts.</p>
        ) : (
          <ul className="alert-list" aria-labelledby="alerts-heading">
            {ranked.slice(0, 60).map((state) => {
              const index = nodeIndex?.get(state.node_id);
              const node = index !== undefined ? topology.nodes[index] : null;
              const style = SEVERITY_STYLE[state.severity];
              return (
                <li key={state.node_id}>
                  <button
                    type="button"
                    className={`alert-row sev-${state.severity} ${selectedId === state.node_id ? "is-selected" : ""}`}
                    onClick={() => onSelect(state.node_id)}
                    aria-current={selectedId === state.node_id ? "true" : undefined}
                    data-testid={`alert-row-${state.node_id}`}
                    data-severity={state.severity}
                  >
                    <span className="sev-glyph" aria-hidden="true">{style.glyph}</span>
                    <span className="alert-row-main">
                      <span className="alert-row-name">
                        {DEVICE_GLYPH[node?.device_type] || "○"} {node?.name || state.node_id}
                      </span>
                      <span className="alert-row-meta">
                        {node?.ip} · {state.alert_count} alert{state.alert_count === 1 ? "" : "s"}
                        {state.top_ports?.length ? ` · port ${state.top_ports[0]}` : ""}
                      </span>
                    </span>
                    <span className="alert-row-sev">{style.label}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
        {ranked.length > 0 && (
          <button type="button" className="btn btn-ghost" onClick={onAcknowledge}>
            Acknowledge worst
          </button>
        )}
      </div>

      <details className="panel-section" open>
        <summary className="panel-title">Filters</summary>

        <fieldset className="filter-group">
          <legend>Minimum severity</legend>
          <div className="chip-row">
            {SEVERITIES.slice(1).map((severity) => (
              <button
                key={severity}
                type="button"
                className={`chip sev-${severity} ${filter.minSeverity === severity ? "is-on" : ""}`}
                aria-pressed={filter.minSeverity === severity}
                onClick={() => onFilterChange({
                  minSeverity: filter.minSeverity === severity ? null : severity,
                })}
              >
                {severity}
              </button>
            ))}
          </div>
        </fieldset>

        <FacetGroup
          legend="Subnet" items={facets.subnets} selected={filter.subnets}
          onToggle={(v) => toggle("subnets", v)} limit={12}
        />
        <FacetGroup
          legend="Device type" items={facets.types} selected={filter.deviceTypes}
          onToggle={(v) => toggle("deviceTypes", v)} limit={10}
        />
        <FacetGroup
          legend="Vendor" items={facets.vendors} selected={filter.vendors}
          onToggle={(v) => toggle("vendors", v)} limit={10}
        />

        <button
          type="button"
          className="btn btn-ghost"
          onClick={() => onFilterChange({
            subnets: new Set(), vendors: new Set(), deviceTypes: new Set(), minSeverity: null,
          })}
        >
          Clear filters
        </button>
      </details>
    </aside>
  );
}

function FacetGroup({ legend, items, selected, onToggle, limit }) {
  return (
    <fieldset className="filter-group">
      <legend>{legend}</legend>
      <div className="chip-row">
        {items.slice(0, limit).map(([value, count]) => (
          <button
            key={value}
            type="button"
            className={`chip ${selected?.has(value) ? "is-on" : ""}`}
            aria-pressed={Boolean(selected?.has(value))}
            onClick={() => onToggle(value)}
            title={`${count} device${count === 1 ? "" : "s"}`}
          >
            {value || "unknown"} <span className="chip-count">{count}</span>
          </button>
        ))}
      </div>
    </fieldset>
  );
}
