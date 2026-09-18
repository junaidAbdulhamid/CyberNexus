import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DEMO_MODE, createClients } from "./lib/api.js";
import { AlertStore } from "./lib/store.js";
import { PALETTES, SEVERITY_STYLE, applyPaletteToCss, getPalette } from "./lib/theme.js";
import MapView from "./components/MapView.jsx";
import Sidebar from "./components/Sidebar.jsx";
import NodeDetail from "./components/NodeDetail.jsx";
import Timeline from "./components/Timeline.jsx";
import StatusBar from "./components/StatusBar.jsx";
import IncidentBanner from "./components/IncidentBanner.jsx";
import KpiStrip from "./components/KpiStrip.jsx";
import CommandPalette from "./components/CommandPalette.jsx";
import Toasts from "./components/Toasts.jsx";
import BootSequence from "./components/BootSequence.jsx";
import Icon from "./components/Icon.jsx";
import { KeyboardHelp, KeyboardNodeList, LiveRegion } from "./components/A11y.jsx";
import LogView from "./baseline/LogView.jsx";

const PALETTE_ORDER = ["default", "colorblind", "highContrast"];
const RANK = { none: 0, info: 1, low: 2, medium: 3, high: 4, critical: 5 };

export default function App() {
  // The transport is chosen at startup: HTTP + WebSocket against the backend,
  // or an in-browser engine for the static demo build. Both satisfy the same
  // interface, so nothing below this line knows which one it got.
  const [clients, setClients] = useState(null);
  const api = clients?.api ?? null;
  const store = useMemo(() => new AlertStore(), []);
  const sceneRef = useRef(null);
  const searchRef = useRef(null);

  const [topology, setTopology] = useState(null);
  const [connection, setConnection] = useState({ state: "connecting", attempt: 0 });
  const [selectedId, setSelectedId] = useState(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState({
    subnets: new Set(), vendors: new Set(), deviceTypes: new Set(), minSeverity: null, query: "",
  });
  const [paletteName, setPaletteName] = useState(
    () => localStorage.getItem("cnmap.palette") || "default"
  );
  const [view, setView] = useState("map");
  const [lod, setLod] = useState(true);
  const [helpOpen, setHelpOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [sceneStats, setSceneStats] = useState({
    fps: 0, drawCalls: 0, nodesRendered: 0, clusters: 0, animated: 0, suppressed: 0,
  });
  const [serverStats, setServerStats] = useState(null);
  const [announcements, setAnnouncements] = useState([]);
  const [toasts, setToasts] = useState([]);
  const [live, setLive] = useState(true);
  const [booting, setBooting] = useState(true);
  const [boot, setBoot] = useState({ topology: false, scene: false, stream: false, ready: false });
  const [fatalError, setFatalError] = useState(null);
  const [, forceRender] = useState(0);

  // React re-renders at the store's flush rate, not once per alert.
  useEffect(() => store.subscribe(() => forceRender((n) => n + 1)), [store]);

  useEffect(() => {
    applyPaletteToCss(getPalette(paletteName));
    localStorage.setItem("cnmap.palette", paletteName);
  }, [paletteName]);

  const pushToast = useCallback((toast) => {
    const id = Math.random().toString(36).slice(2);
    setToasts((prev) => [...prev.slice(-3), { id, ...toast }]);
  }, []);

  useEffect(() => {
    let cancelled = false;
    createClients()
      .then((resolved) => { if (!cancelled) setClients(resolved); })
      .catch((error) => { if (!cancelled) setFatalError(error.message); });
    return () => { cancelled = true; };
  }, []);

  // -- data ------------------------------------------------------------
  useEffect(() => {
    if (!api) return undefined;
    let cancelled = false;
    api.topology()
      .then((data) => {
        if (cancelled) return;
        setTopology(data);
        setBoot((b) => ({ ...b, topology: true }));
      })
      .catch((error) => {
        pushToast({ kind: "error", title: "Could not load topology", body: error.message });
        setBoot((b) => ({ ...b, topology: true }));
      });
    api.states().then((data) => {
      if (!cancelled && data.states?.length) store.applySnapshot(data);
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [api, store, pushToast]);

  useEffect(() => {
    if (!api) return undefined;
    const poll = () => api.stats().then(setServerStats).catch(() => {});
    poll();
    const id = setInterval(poll, 5000);
    return () => clearInterval(id);
  }, [api]);

  const announce = useCallback((state, node) => {
    const style = SEVERITY_STYLE[state.severity];
    const message =
      `${style.label} severity alert on ${node?.name || state.node_id}` +
      `${node?.ip ? `, address ${node.ip}` : ""}, ${state.alert_count} alerts.`;
    setAnnouncements((prev) => [{ message, severity: state.severity, ts: Date.now() }, ...prev].slice(0, 20));
  }, []);

  useEffect(() => {
    if (!clients) return undefined;
    const stream = new clients.Stream({
      onStatus: (status) => {
        setConnection(status);
        if (status.state === "open") setBoot((b) => ({ ...b, stream: true }));
      },
      onMessage: (frame) => {
        if (frame.type === "snapshot") {
          store.applySnapshot(frame);
        } else if (frame.type === "node_state") {
          const previous = store.states.get(frame.state.node_id);
          store.applyUpdate(frame);
          // Announce genuine escalations only: a screen reader must not read out
          // every repeat of an alert already on the board.
          if (!previous || RANK[frame.state.severity] > RANK[previous.severity]) {
            const index = topology?.index.get(frame.state.node_id);
            announce(frame.state, index !== undefined ? topology.nodes[index] : null);
          }
        } else if (frame.type === "topology_changed") {
          api.topology().then(setTopology).catch(() => {});
        }
      },
    });
    stream.connect();
    return () => stream.close();
  }, [clients, api, store, topology, announce]);

  useEffect(() => {
    if (boot.topology && boot.scene && boot.stream) {
      const id = setTimeout(() => setBoot((b) => ({ ...b, ready: true })), 180);
      return () => clearTimeout(id);
    }
  }, [boot.topology, boot.scene, boot.stream]);

  // -- actions ---------------------------------------------------------
  const selectNode = useCallback((nodeId, { focus = true } = {}) => {
    setSelectedId(nodeId);
    if (nodeId && focus) sceneRef.current?.focusNode(nodeId);
  }, []);

  const focusWorst = useCallback(() => {
    const worst = store.worst();
    if (worst) selectNode(worst.node_id);
    else pushToast({ title: "Nothing to jump to", body: "No hosts are alerting." });
  }, [store, selectNode, pushToast]);

  const acknowledge = useCallback(async (nodeId) => {
    const id = nodeId || selectedId || store.worst()?.node_id;
    if (!id || !api) return;
    try {
      await api.acknowledge(id);
      store.states.delete(id);
      store.onSceneUpdate?.(null, null);
      forceRender((n) => n + 1);
      pushToast({ kind: "success", title: "Acknowledged", body: id });
    } catch (error) {
      pushToast({ kind: "error", title: "Could not acknowledge", body: error.message });
    }
  }, [api, selectedId, store, pushToast]);

  const createTicket = useCallback(async (nodeId) => {
    const id = nodeId || selectedId;
    if (!api) return;
    if (!id) {
      pushToast({ title: "Select a host first" });
      return;
    }
    try {
      const result = await api.createTicket(id);
      pushToast({
        kind: result.status === "failed" ? "error" : "success",
        title: result.status === "dry-run" ? "Ticket prepared (dry run)" : "Ticket raised",
        body: result.ticket_ref || id,
      });
    } catch (error) {
      pushToast({ kind: "error", title: "Ticket failed", body: error.message });
    }
  }, [api, selectedId, pushToast]);

  const replayAt = useCallback(async (ts) => {
    if (!api) return;
    setLive(false);
    const data = await api.replay(ts).catch(() => null);
    if (data) store.setStates(data.states);
  }, [api, store]);

  const goLive = useCallback(async () => {
    if (!api) return;
    setLive(true);
    const data = await api.states().catch(() => null);
    if (data) store.setStates(data.states);
  }, [api, store]);

  const cyclePalette = useCallback(() => {
    setPaletteName((current) =>
      PALETTE_ORDER[(PALETTE_ORDER.indexOf(current) + 1) % PALETTE_ORDER.length]);
  }, []);

  const paletteActions = useMemo(() => ({
    select: (id) => selectNode(id),
    focusWorst,
    frameAll: () => sceneRef.current?.frameAll(),
    acknowledge: () => acknowledge(),
    ticket: () => createTicket(),
    toggleView: () => setView((v) => (v === "map" ? "log" : "map")),
    cyclePalette,
    toggleLod: () => setLod((v) => !v),
    exportCsv: () => api && window.open(api.exportUrl("csv"), "_blank"),
    showHelp: () => setHelpOpen(true),
  }), [selectNode, focusWorst, acknowledge, createTicket, cyclePalette, api]);

  // -- keyboard --------------------------------------------------------
  useEffect(() => {
    const onKey = (event) => {
      const inField = ["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName);

      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((open) => !open);
        return;
      }
      if (event.key === "Escape") {
        if (paletteOpen) setPaletteOpen(false);
        else if (helpOpen) setHelpOpen(false);
        else if (selectedId) setSelectedId(null);
        else event.target.blur?.();
        return;
      }
      if (inField || paletteOpen) return;

      const ranked = store.ranked();
      const currentIndex = ranked.findIndex((s) => s.node_id === selectedId);
      switch (event.key) {
        case "/":
          event.preventDefault();
          searchRef.current?.focus();
          break;
        case "?":
          setHelpOpen(true);
          break;
        case "n": case "ArrowDown":
          if (ranked.length) {
            event.preventDefault();
            selectNode(ranked[Math.min(currentIndex + 1, ranked.length - 1)].node_id);
          }
          break;
        case "p": case "ArrowUp":
          if (ranked.length) {
            event.preventDefault();
            selectNode(ranked[Math.max(currentIndex - 1, 0)].node_id);
          }
          break;
        case "Enter": {
          const id = selectedId || store.worst()?.node_id;
          if (id) { setSelectedId(id); sceneRef.current?.focusNode(id); }
          break;
        }
        case "w": focusWorst(); break;
        case "a": acknowledge(selectedId); break;
        case "f": sceneRef.current?.frameAll(); break;
        case "l": setLod((v) => !v); break;
        case "b": setView((v) => (v === "map" ? "log" : "map")); break;
        case "c": cyclePalette(); break;
        default: break;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [store, selectedId, helpOpen, paletteOpen, selectNode, focusWorst, acknowledge, cyclePalette]);

  useEffect(() => { setFilter((f) => ({ ...f, query })); }, [query]);

  // Test/automation hook. The MTTI harness and the Playwright suites read this
  // rather than scraping the canvas; it is inert in normal use.
  useEffect(() => {
    window.__cnmap = {
      getAlertingNodes: () => store.ranked().map((s) => ({ ...s })),
      getWorst: () => store.worst(),
      getSelected: () => selectedId,
      getSceneStats: () => sceneRef.current?.stats ?? null,
      getTopologySize: () => topology?.nodes.length ?? 0,
      getLastUpdateAt: () => store.lastUpdateAt,
      getCounters: () => ({ ...store.counters }),
      selectNode,
      view,
    };
  }, [store, selectedId, topology, view, selectNode]);

  const worstState = store.worst();
  const worstIndex = worstState ? topology?.index.get(worstState.node_id) : undefined;
  const worstNode = worstIndex !== undefined ? topology.nodes[worstIndex] : null;

  const storeStats = {
    alerts: store.counters.alerts,
    sinceLastUpdate: store.lastUpdateAt ? performance.now() - store.lastUpdateAt : null,
  };

  return (
    <div className={`app palette-${paletteName}`} data-palette={paletteName} data-view={view}>
      <a href="#main" className="skip-link">Skip to map</a>

      {booting && (
        <BootSequence progress={boot} error={fatalError} onDone={() => setBooting(false)} />
      )}

      <header className="topbar">
        <h1 className="brand">
          <span className="brand-mark"><Icon name="shield" size={15} strokeWidth={2} /></span>
          <span className="brand-text">
            <span className="brand-name">CyberNexus</span>
            <span className="brand-sub">live network map</span>
          </span>
        </h1>

        <nav className="view-switch" aria-label="View">
          <button
            type="button" className={view === "map" ? "is-on" : ""}
            aria-pressed={view === "map"} onClick={() => setView("map")}
            data-testid="view-map"
          >
            3D map
          </button>
          <button
            type="button" className={view === "log" ? "is-on" : ""}
            aria-pressed={view === "log"} onClick={() => setView("log")}
            data-testid="view-log"
          >
            Alert log
          </button>
        </nav>

        <div className="topbar-actions">
          <button
            type="button" className="btn btn-small" onClick={() => setPaletteOpen(true)}
            data-testid="open-palette"
          >
            <Icon name="search" size={12} /> Search <kbd>⌘K</kbd>
          </button>
          <label className="field-inline">
            <Icon name="eye" size={12} />
            <select
              value={paletteName} onChange={(e) => setPaletteName(e.target.value)}
              aria-label="Colour palette"
            >
              {PALETTE_ORDER.map((name) => (
                <option key={name} value={name}>{PALETTES[name].label}</option>
              ))}
            </select>
          </label>
          <label className="checkbox">
            <input type="checkbox" checked={lod} onChange={(e) => setLod(e.target.checked)} />
            Clustering
          </label>
          <button type="button" className="btn-icon" onClick={() => setHelpOpen(true)} aria-label="Keyboard shortcuts">
            <Icon name="keyboard" size={16} />
          </button>
          {DEMO_MODE && (
            <span className="demo-badge" title={
              "Static build: alerts are generated in your browser. The live build "
              + "consumes Stage 1 detections from a Redis stream over a WebSocket."
            }>
              <Icon name="zap" size={11} /> demo data
            </span>
          )}
        </div>
      </header>

      <KpiStrip
        states={store} topology={topology} stats={serverStats}
        connection={connection} sceneStats={sceneStats}
      />

      <div className="layout">
        <Sidebar
          topology={topology} states={store} filter={filter}
          onFilterChange={(patch) => setFilter((f) => ({ ...f, ...patch }))}
          query={query} onQueryChange={setQuery}
          selectedId={selectedId} onSelect={selectNode}
          onAcknowledge={() => acknowledge()}
          searchRef={searchRef}
          onOpenPalette={() => setPaletteOpen(true)}
        />

        <main id="main" className="main" tabIndex={-1}>
          <IncidentBanner
            state={worstState}
            node={worstNode}
            onFocus={(id) => { setSelectedId(id); sceneRef.current?.focusNode(id); }}
            onAcknowledge={acknowledge}
            onTicket={createTicket}
            onOpen={(id) => selectNode(id, { focus: false })}
          />
          {view === "map" ? (
            <MapView
              topology={topology} store={store} paletteName={paletteName}
              selectedId={selectedId} filter={filter} lod={lod}
              onSelect={(id) => selectNode(id, { focus: false })}
              onSceneReady={(scene) => {
                sceneRef.current = scene;
                setBoot((b) => ({ ...b, scene: true }));
              }}
              onStats={setSceneStats}
            />
          ) : (
            api && <LogView api={api} onSelect={(id) => selectNode(id, { focus: false })} selectedId={selectedId} />
          )}
          {api && <Timeline api={api} onReplay={replayAt} onLive={goLive} live={live} />}
        </main>

        {selectedId && api && (
          <NodeDetail
            api={api} nodeId={selectedId}
            onClose={() => setSelectedId(null)}
            onTicket={createTicket}
            onAcknowledge={acknowledge}
            onFocus={(id) => sceneRef.current?.focusNode(id)}
          />
        )}
      </div>

      <StatusBar
        connection={connection} sceneStats={sceneStats} storeStats={storeStats}
        topology={topology} lodActive={sceneStats.lodActive}
      />

      <CommandPalette
        open={paletteOpen} onClose={() => setPaletteOpen(false)}
        topology={topology} states={store} actions={paletteActions}
      />
      <Toasts toasts={toasts} onDismiss={(id) => setToasts((t) => t.filter((x) => x.id !== id))} />
      <LiveRegion announcements={announcements} />
      <KeyboardNodeList
        states={store} topology={topology} onSelect={selectNode} selectedId={selectedId}
      />
      <KeyboardHelp open={helpOpen} onClose={() => setHelpOpen(false)} />
    </div>
  );
}
