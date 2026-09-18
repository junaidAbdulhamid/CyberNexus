import React, { useMemo } from "react";
import { SEVERITIES, SEVERITY_STYLE } from "../lib/theme.js";
import Icon, { DeviceIcon } from "./Icon.jsx";

/**
 * Left rail: search, the ranked alert queue, and filters.
 *
 * The queue is the keyboard-first path to everything the 3D view shows. Every
 * row is a real button, ordered worst-first, and selecting one flies the camera
 * there. Someone who cannot use a pointer — or who simply prefers a list — is
 * never locked out of the map.
 */
export default function Sidebar({
  topology, states, filter, onFilterChange, query, onQueryChange,
  selectedId, onSelect, onAcknowledge, searchRef, onOpenPalette,
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
  const criticalCount = ranked.filter((s) => s.severity === "critical").length;

  const toggle = (key, value) => {
    const next = new Set(filter[key] || []);
    next.has(value) ? next.delete(value) : next.add(value);
    onFilterChange({ [key]: next });
  };

  const activeFilters =
    (filter.subnets?.size || 0) + (filter.vendors?.size || 0) +
    (filter.deviceTypes?.size || 0) + (filter.minSeverity ? 1 : 0);

  return (
    <aside className="sidebar" aria-label="Alerts and filters">
      <div className="panel-section">
        <label className="field-label" htmlFor="node-search">Search</label>
        <div className="search-wrap">
          <Icon name="search" size={13} className="search-icon" />
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
          <kbd className="search-kbd">/</kbd>
        </div>
        <p id="search-help" className="hint">
          Filters the map. Alerting hosts always stay visible.{" "}
          <button type="button" className="link-button" onClick={onOpenPalette}>
            ⌘K for commands
          </button>
        </p>
      </div>

      <div className="panel-section">
        <h2 className="panel-title" id="alerts-heading">
          <Icon name="alert" size={11} />
          Alert queue
          <span className={`count-badge ${criticalCount > 0 ? "is-hot" : ""}`}>{ranked.length}</span>
        </h2>

        {ranked.length === 0 ? (
          <p className="empty">
            <Icon name="check" size={20} className="empty-icon" />
            No active alerts.<br />
            <span className="faint">The network is quiet.</span>
          </p>
        ) : (
          <div className="alert-list-scroll">
            <ul className="alert-list" aria-labelledby="alerts-heading">
              {ranked.slice(0, 80).map((state) => {
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
                          <DeviceIcon type={node?.device_type} size={12} />
                          {node?.name || state.node_id}
                        </span>
                        {/* Compact on purpose: at rail width the long form
                            ("5 alerts · port 21") truncated mid-word, which
                            reads as a bug rather than as elision. */}
                        <span className="alert-row-meta">
                          {node?.ip}
                          <span className="faint"> · </span>{state.alert_count}×
                          {state.top_ports?.length ? (
                            <><span className="faint"> · </span>:{state.top_ports[0]}</>
                          ) : null}
                        </span>
                      </span>
                      <span className="alert-row-sev">{style.label}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        )}

        {ranked.length > 0 && (
          <button type="button" className="btn btn-ghost" onClick={onAcknowledge}>
            <Icon name="check" size={12} /> Acknowledge worst
          </button>
        )}
      </div>

      <details className="panel-section" open>
        <summary className="panel-title">
          <Icon name="filter" size={11} />
          Filters
          {activeFilters > 0 && <span className="count-badge">{activeFilters}</span>}
        </summary>

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
          onToggle={(v) => toggle("subnets", v)} limit={10}
        />
        <FacetGroup
          legend="Device type" items={facets.types} selected={filter.deviceTypes}
          onToggle={(v) => toggle("deviceTypes", v)} limit={9}
        />
        <FacetGroup
          legend="Vendor" items={facets.vendors} selected={filter.vendors}
          onToggle={(v) => toggle("vendors", v)} limit={8}
        />

        {activeFilters > 0 && (
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => onFilterChange({
              subnets: new Set(), vendors: new Set(), deviceTypes: new Set(), minSeverity: null,
            })}
          >
            <Icon name="x" size={12} /> Clear all filters
          </button>
        )}
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
            {String(value || "unknown").replace(/_/g, " ")}
            <span className="chip-count">{count}</span>
          </button>
        ))}
      </div>
    </fieldset>
  );
}
