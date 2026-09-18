import React, { useEffect, useRef, useState } from "react";
import { NetworkScene } from "../three/NetworkScene.js";
import { SEVERITIES, SEVERITY_STYLE } from "../lib/theme.js";
import Icon, { DeviceIcon } from "./Icon.jsx";

/**
 * Canvas host. Owns the `NetworkScene` lifetime and bridges it to React.
 *
 * React never drives the render loop: the scene runs its own rAF loop and is
 * mutated imperatively. React hands it data and receives selection events, and
 * that separation is what keeps a 60 fps canvas and a re-rendering component
 * tree from fighting each other.
 */
export default function MapView({
  topology, store, paletteName, selectedId, filter, lod,
  onSelect, onSceneReady, onStats,
}) {
  const canvasRef = useRef(null);
  const sceneRef = useRef(null);
  const [hover, setHover] = useState(null);
  const [pointer, setPointer] = useState({ x: 0, y: 0 });
  const [hudStats, setHudStats] = useState({ nodesRendered: 0, clusters: 0, lodActive: false });

  useEffect(() => {
    const scene = new NetworkScene(canvasRef.current, {
      paletteName,
      onSelect,
      onHover: (hit) => setHover(hit ? hit.node : null),
    });
    sceneRef.current = scene;
    onSceneReady?.(scene);

    // The store pushes straight into the scene, bypassing React entirely, so a
    // node is repainted in the same tick its update arrives.
    store.onSceneUpdate = (nodeId, state) => {
      if (nodeId) scene.updateState(nodeId, state);
      else scene.setStates(store.states);
    };

    const statsTimer = setInterval(() => {
      const next = { ...scene.stats, lodActive: scene.lodActive };
      setHudStats(next);
      onStats?.(next);
    }, 500);

    return () => {
      clearInterval(statsTimer);
      store.onSceneUpdate = null;
      scene.dispose();
      sceneRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (topology && sceneRef.current) {
      sceneRef.current.setTopology(topology);
      sceneRef.current.setStates(store.states);
    }
  }, [topology, store]);

  useEffect(() => { sceneRef.current?.setPalette(paletteName); }, [paletteName]);
  useEffect(() => { sceneRef.current?.setSelected(selectedId); }, [selectedId]);
  useEffect(() => { sceneRef.current?.setFilter(filter); }, [filter]);
  useEffect(() => { sceneRef.current?.setLod(lod); }, [lod]);

  return (
    <div className="mapview" onPointerMove={(e) => setPointer({ x: e.clientX, y: e.clientY })}>
      <canvas
        ref={canvasRef}
        className="map-canvas"
        data-testid="map-canvas"
        aria-label="3D network map. An equivalent keyboard-accessible list of alerting hosts is provided alongside it."
        role="img"
      />

      <div className="map-overlay">
        <div className="map-hud">
          <span className="hud-chip is-live">
            <Icon name="live" size={11} /> live
          </span>
          <span className="hud-chip">
            <strong>{hudStats.nodesRendered}</strong> drawn
          </span>
          {hudStats.lodActive && (
            <span className="hud-chip">
              <Icon name="layers" size={11} /> <strong>{hudStats.clusters}</strong> districts
            </span>
          )}
          <span className="hud-chip">drag to orbit · scroll to zoom</span>
        </div>

        <Legend />
      </div>

      {hover && (
        <div className="hover-card" style={{ left: pointer.x + 16, top: pointer.y + 16 }} role="tooltip">
          <strong>
            <DeviceIcon type={hover.device_type} size={12} />
            {hover.name}
          </strong>
          <span className="mono">{hover.ip}</span>
          <span className="mono">{hover.vendor} · {hover.subnet}</span>
        </div>
      )}
    </div>
  );
}

function Legend() {
  return (
    <div className="legend" aria-hidden="true">
      <span className="legend-title">Severity</span>
      {SEVERITIES.slice(1).map((severity) => (
        <span key={severity} className="legend-item">
          <span className={`legend-swatch sev-bg-${severity}`} style={{ color: `var(--sev-${severity})` }} />
          <span className={`legend-glyph sev-${severity}`}>{SEVERITY_STYLE[severity].glyph}</span>
          {severity}
        </span>
      ))}
      <span className="legend-note">size, glow and pulse rate encode severity too — never colour alone</span>
    </div>
  );
}
