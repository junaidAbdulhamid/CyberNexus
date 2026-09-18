import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { MapApi, MapStream } from "./lib/api.js";
import { AlertStore } from "./lib/store.js";
import { PALETTES, SEVERITY_STYLE, applyPaletteToCss, getPalette } from "./lib/theme.js";
import MapView from "./components/MapView.jsx";
import Sidebar from "./components/Sidebar.jsx";
import NodeDetail from "./components/NodeDetail.jsx";
import Timeline from "./components/Timeline.jsx";
import StatusBar from "./components/StatusBar.jsx";
import IncidentBanner from "./components/IncidentBanner.jsx";
import { KeyboardHelp, KeyboardNodeList, LiveRegion } from "./components/A11y.jsx";
import LogView from "./baseline/LogView.jsx";

const PALETTE_ORDER = ["default", "colorblind", "highContrast"];

export default function App() {
  const api = useMemo(() => new MapApi(), []);
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
  const [view, setView] = useState("map");          // "map" | "log"
  const [lod, setLod] = useState(true);
  const [helpOpen, setHelpOpen] = useState(false);
  const [sceneStats, setSceneStats] = useState({ fps: 0, drawCalls: 0, nodesRendered: 0, clusters: 0 });
  const [announcements, setAnnouncements] = useState([]);
  const [live, setLive] = useState(true);
  const [, forceRender] = useState(0);

  // React re-renders at the store's flush rate, not per alert.
  useEffect(() => store.subscribe(() => forceRender((n) => n + 1)), [store]);

  useEffect(() => {
    applyPaletteToCss(getPalette(paletteName));
    localStorage.setItem("cnmap.palette", paletteName);
  }, [paletteName]);

  // -- data ------------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    api.topology().then((data) => { if (!cancelled) setTopology(data); }).catch(console.error);
    api.states().then((data) => {
      if (!cancelled && data.states?.length) store.applySnapshot(data);
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [api, store]);

  const announce = useCallback((state, node) => {
    const style = SEVERITY_STYLE[state.severity];
    const message =
      `${style.label} severity alert on ${node?.name || state.node_id}` +
      `${node?.ip ? `, address ${node.ip}` : ""}, ${state.alert_count} alerts.`;
    setAnnouncements((prev) => [{ message, severity: state.severity, ts: Date.now() }, ...prev].slice(0, 20));
  }, []);

  useEffect(() => {
    const stream = new MapStream({
      onStatus: setConnection,
      onMessage: (frame) => {
        if (frame.type === "snapshot") {
          store.applySnapshot(frame);
        } else if (frame.type === "node_state") {
          const previous = store.states.get(frame.state.node_id);
          store.applyUpdate(frame);
          // Announce only genuine escalations, or a screen reader would read
          // out every repeat of an alert that is already on the board.
          const rank = { none: 0, info: 1, low: 2, medium: 3, high: 4, critical: 5 };
          if (!previous || rank[frame.state.severity] > rank[previous.severity]) {
            const index = topology?.index.get(frame.state.node_id);
            announce(frame.state, index !== undefined ? topology.nodes[index] : null);
          }
        } else if (frame.type === "topology_changed") {
          api.topology().then(setTopology).catch(console.error);
        }
      },
    });
    stream.connect();
    return () => stream.close();
  }, [api, store, topology, announce]);

  // -- actions ---------------------------------------------------------
  const selectNode = useCallback((nodeId, { focus = true } = {}) => {
    setSelectedId(nodeId);
    if (nodeId && focus) sceneRef.current?.focusNode(nodeId);
  }, []);

  const focusWorst = useCallback(() => {
    const worst = store.worst();
    if (worst) selectNode(worst.node_id);
  }, [store, selectNode]);

  const acknowledge = useCallback(async (nodeId) => {
    const id = nodeId || selectedId || store.worst()?.node_id;
    if (!id) return;
    await api.acknowledge(id).catch(console.error);
    store.states.delete(id);
    store.onSceneUpdate?.(null, null);
    forceRender((n) => n + 1);
  }, [api, selectedId, store]);

  const createTicket = useCallback(async (nodeId) => {
    const result = await api.createTicket(nodeId).catch((e) => ({ status: "failed", error: e.message }));
    setAnnouncements((prev) => [{
      message: `Ticket ${result.status}${result.ticket_ref ? ` (${result.ticket_ref})` : ""}`,
      severity: "info", ts: Date.now(),
    }, ...prev].slice(0, 20));
  }, [api]);

  const replayAt = useCallback(async (ts) => {
    setLive(false);
    const data = await api.replay(ts).catch(() => null);
    if (data) store.setStates(data.states);
  }, [api, store]);

  const goLive = useCallback(async () => {
    setLive(true);
    const data = await api.states().catch(() => null);
    if (data) store.setStates(data.states);
  }, [api, store]);

  // -- keyboard --------------------------------------------------------
  useEffect(() => {
    const onKey = (event) => {
      const inField = ["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName);
      if (event.key === "Escape") {
        if (helpOpen) setHelpOpen(false);
        else if (selectedId) setSelectedId(null);
        else event.target.blur?.();
        return;
      }
      if (inField) return;
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
          // With nothing selected, Enter acts on the banner - the worst
          // incident - so the keyboard path matches what is on screen.
          const id = selectedId || store.worst()?.node_id;
          if (id) { setSelectedId(id); sceneRef.current?.focusNode(id); }
          break;
        }
        case "w": focusWorst(); break;
        case "a": if (selectedId) acknowledge(selectedId); break;
        case "f": sceneRef.current?.frameAll(); break;
        case "l": setLod((v) => !v); break;
        case "b": setView((v) => (v === "map" ? "log" : "map")); break;
        case "c":
          setPaletteName((current) =>
            PALETTE_ORDER[(PALETTE_ORDER.indexOf(current) + 1) % PALETTE_ORDER.length]);
          break;
        default: break;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [store, selectedId, helpOpen, selectNode, focusWorst, acknowledge]);

  useEffect(() => {
    setFilter((f) => ({ ...f, query }));
  }, [query]);

  // Test/automation hook. The MTTI harness and the Playwright tests read this
  // instead of scraping the canvas; it is inert in normal use.
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

      <header className="topbar">
        <h1 className="brand">CyberNexus <span className="brand-dim">live map</span></h1>
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
          <label className="field-inline">
            Palette
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
          <button type="button" className="btn btn-small" onClick={() => setHelpOpen(true)}>
            Shortcuts <kbd>?</kbd>
          </button>
        </div>
      </header>

      <div className="layout">
        <Sidebar
          topology={topology} states={store} filter={filter}
          onFilterChange={(patch) => setFilter((f) => ({ ...f, ...patch }))}
          query={query} onQueryChange={setQuery}
          selectedId={selectedId} onSelect={selectNode}
          onAcknowledge={() => acknowledge()}
          searchRef={searchRef}
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
              onSceneReady={(scene) => { sceneRef.current = scene; }}
              onStats={setSceneStats}
            />
          ) : (
            <LogView api={api} onSelect={(id) => selectNode(id, { focus: false })} selectedId={selectedId} />
          )}
          <Timeline api={api} onReplay={replayAt} onLive={goLive} live={live} />
        </main>

        {selectedId && (
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

      <LiveRegion announcements={announcements} />
      <KeyboardNodeList
        states={store} topology={topology} onSelect={selectNode} selectedId={selectedId}
      />
      <KeyboardHelp open={helpOpen} onClose={() => setHelpOpen(false)} />
    </div>
  );
}
