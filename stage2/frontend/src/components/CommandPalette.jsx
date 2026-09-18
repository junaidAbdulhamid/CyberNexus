import React, { useEffect, useMemo, useRef, useState } from "react";
import Icon from "./Icon.jsx";
import { SEVERITY_STYLE } from "../lib/theme.js";

/**
 * ⌘K command palette.
 *
 * On a map of several hundred hosts, the fastest route to a *named* host is
 * typing its name — panning and clicking is for exploring, not for answering
 * "what is going on with hq-finance-ws-014". The palette merges three things
 * into one ranked list: active alerts first (because that is what an operator is
 * usually reaching for), then any host in the topology, then the actions.
 *
 * It is also the discoverability surface for the keyboard shortcuts: every
 * action shows its key, so using the palette teaches you to stop needing it.
 */
export default function CommandPalette({ open, onClose, topology, states, actions }) {
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef(null);
  const listRef = useRef(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setCursor(0);
      // rAF so the element exists before focus, avoiding a lost first keystroke
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  const results = useMemo(() => {
    if (!open) return [];
    const q = query.trim().toLowerCase();
    const out = [];

    const alerting = states.ranked();
    const alertItems = alerting
      .map((state) => {
        const index = topology?.index.get(state.node_id);
        const node = index !== undefined ? topology.nodes[index] : null;
        return node ? { state, node } : null;
      })
      .filter(Boolean)
      .filter(({ node }) => !q || matches(node, q))
      .slice(0, 6)
      .map(({ state, node }) => ({
        group: "Active alerts",
        id: `alert:${node.id}`,
        title: node.name,
        sub: `${node.ip} · ${state.alert_count} alert${state.alert_count === 1 ? "" : "s"} · ${SEVERITY_STYLE[state.severity].label}`,
        severity: state.severity,
        run: () => actions.select(node.id),
      }));
    out.push(...alertItems);

    if (q && topology) {
      const seen = new Set(alertItems.map((item) => item.id.split(":")[1]));
      const hosts = topology.nodes
        .filter((node) => !seen.has(node.id) && matches(node, q))
        .slice(0, 8)
        .map((node) => ({
          group: "Hosts",
          id: `host:${node.id}`,
          title: node.name,
          sub: `${node.ip || "no address"} · ${node.device_type} · ${node.subnet}`,
          run: () => actions.select(node.id),
        }));
      out.push(...hosts);
    }

    const commands = [
      { id: "cmd:worst", title: "Jump to worst alert", hint: "w", icon: "target", run: actions.focusWorst },
      { id: "cmd:frame", title: "Frame entire network", hint: "f", icon: "network", run: actions.frameAll },
      { id: "cmd:ack", title: "Acknowledge selected host", hint: "a", icon: "check", run: actions.acknowledge },
      { id: "cmd:ticket", title: "Create ticket for selected host", hint: "", icon: "ticket", run: actions.ticket },
      { id: "cmd:log", title: "Switch to alert log view", hint: "b", icon: "layers", run: actions.toggleView },
      { id: "cmd:palette", title: "Cycle colour palette", hint: "c", icon: "eye", run: actions.cyclePalette },
      { id: "cmd:lod", title: "Toggle clustering", hint: "l", icon: "layers", run: actions.toggleLod },
      { id: "cmd:export", title: "Export alert timeline (CSV)", hint: "", icon: "download", run: actions.exportCsv },
      { id: "cmd:shortcuts", title: "Show keyboard shortcuts", hint: "?", icon: "keyboard", run: actions.showHelp },
    ].filter((command) => !q || command.title.toLowerCase().includes(q))
      .map((command) => ({ ...command, group: "Commands" }));
    out.push(...commands);

    return out;
  }, [open, query, topology, states, actions]);

  useEffect(() => {
    if (cursor >= results.length) setCursor(Math.max(results.length - 1, 0));
  }, [results.length, cursor]);

  useEffect(() => {
    listRef.current?.querySelector(".is-active")?.scrollIntoView({ block: "nearest" });
  }, [cursor]);

  if (!open) return null;

  const onKeyDown = (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setCursor((c) => Math.min(c + 1, results.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setCursor((c) => Math.max(c - 1, 0));
    } else if (event.key === "Enter") {
      event.preventDefault();
      const item = results[cursor];
      if (item) { item.run(); onClose(); }
    } else if (event.key === "Escape") {
      event.preventDefault();
      onClose();
    }
  };

  let lastGroup = null;

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div
        className="modal palette-modal"
        role="dialog" aria-modal="true" aria-label="Command palette"
        onMouseDown={(e) => e.stopPropagation()}
        data-testid="command-palette"
      >
        <div className="palette-input-row">
          <Icon name="search" size={17} className="faint" />
          <input
            ref={inputRef}
            className="palette-input"
            placeholder="Search hosts, or run a command…"
            value={query}
            onChange={(e) => { setQuery(e.target.value); setCursor(0); }}
            onKeyDown={onKeyDown}
            aria-label="Search hosts or run a command"
            aria-controls="palette-results"
            aria-activedescendant={results[cursor] ? `palette-item-${cursor}` : undefined}
            role="combobox" aria-expanded="true"
            data-testid="palette-input"
          />
          <kbd>esc</kbd>
        </div>

        <div className="palette-results" id="palette-results" role="listbox" ref={listRef}>
          {results.length === 0 && <p className="empty">Nothing matches “{query}”.</p>}
          {results.map((item, i) => {
            const header = item.group !== lastGroup ? item.group : null;
            lastGroup = item.group;
            return (
              <React.Fragment key={item.id}>
                {header && <div className="palette-group-label">{header}</div>}
                <button
                  type="button"
                  id={`palette-item-${i}`}
                  role="option"
                  aria-selected={i === cursor}
                  className={`palette-item ${i === cursor ? "is-active" : ""}`}
                  onMouseEnter={() => setCursor(i)}
                  onClick={() => { item.run(); onClose(); }}
                >
                  {item.severity ? (
                    <span className={`sev-glyph sev-${item.severity}`} style={{ width: 22 }}>
                      {SEVERITY_STYLE[item.severity].glyph}
                    </span>
                  ) : (
                    <Icon name={item.icon || "chevronRight"} size={14} className="faint" />
                  )}
                  <span className="palette-item-main">
                    <span className="palette-item-title">{item.title}</span>
                    {item.sub && <span className="palette-item-sub">{item.sub}</span>}
                  </span>
                  {item.hint && <kbd>{item.hint}</kbd>}
                </button>
              </React.Fragment>
            );
          })}
        </div>

        <div className="palette-foot">
          <span><kbd>↑</kbd><kbd>↓</kbd> navigate</span>
          <span><kbd>↵</kbd> select</span>
          <span><kbd>esc</kbd> dismiss</span>
        </div>
      </div>
    </div>
  );
}

function matches(node, q) {
  return `${node.name} ${node.ip} ${node.mac} ${node.vendor} ${node.subnet} ${node.device_type}`
    .toLowerCase().includes(q);
}
