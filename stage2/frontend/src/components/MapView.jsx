import React, { useEffect, useRef, useState } from "react";
import { NetworkScene } from "../three/NetworkScene.js";
import { DEVICE_GLYPH, SEVERITIES, SEVERITY_STYLE } from "../lib/theme.js";

/**
 * Canvas host. Owns the `NetworkScene` lifetime and bridges it to React.
 *
 * React never drives the render loop: the scene runs its own rAF loop and is
 * mutated imperatively. React only hands it data and receives selection events,
 * which is what keeps a 60 fps canvas and a re-rendering component tree from
 * fighting each other.
 */
export default function MapView({
  topology, store, paletteName, selectedId, filter, lod,
  onSelect, onSceneReady, onStats,
}) {
  const canvasRef = useRef(null);
  const sceneRef = useRef(null);
  const [hover, setHover] = useState(null);
  const [pointer, setPointer] = useState({ x: 0, y: 0 });

  useEffect(() => {
    const scene = new NetworkScene(canvasRef.current, {
      paletteName,
      onSelect,
      onHover: (hit) => setHover(hit ? hit.node : null),
    });
    sceneRef.current = scene;
    onSceneReady?.(scene);

    // The store pushes straight into the scene, bypassing React entirely, so a
    // node is repainted in the same tick the update arrives.
    store.onSceneUpdate = (nodeId, state) => {
      if (nodeId) scene.updateState(nodeId, state);
      else scene.setStates(store.states);
    };

    const statsTimer = setInterval(() => {
      onStats?.({ ...scene.stats, lodActive: scene.lodActive });
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
    <div
      className="mapview"
      onPointerMove={(e) => setPointer({ x: e.clientX, y: e.clientY })}
    >
      <canvas
        ref={canvasRef}
        className="map-canvas"
        data-testid="map-canvas"
        aria-label="3D network map. An equivalent keyboard-accessible list of alerting hosts is provided."
        role="img"
      />
      {hover && (
        <div
          className="hover-card"
          style={{ left: pointer.x + 14, top: pointer.y + 14 }}
          role="tooltip"
        >
          <strong>{DEVICE_GLYPH[hover.device_type]} {hover.name}</strong>
          <span>{hover.ip}</span>
          <span className="dim">{hover.vendor} · {hover.subnet}</span>
        </div>
      )}
      <Legend />
    </div>
  );
}

function Legend() {
  return (
    <div className="legend" aria-hidden="true">
      <span className="legend-title">Severity</span>
      {SEVERITIES.slice(1).map((severity) => (
        <span key={severity} className="legend-item">
          <span className={`legend-swatch sev-bg-${severity}`} />
          <span className="legend-glyph">{SEVERITY_STYLE[severity].glyph}</span>
          {severity}
        </span>
      ))}
      <span className="legend-note">size + pulse also encode severity</span>
    </div>
  );
}
