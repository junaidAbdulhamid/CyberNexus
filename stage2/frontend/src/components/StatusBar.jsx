import React from "react";
import Icon from "./Icon.jsx";

/**
 * The bottom rail: connection state and the render budget.
 *
 * The FPS / draw-call / latency readout is not decoration — it is how the
 * performance requirements are checked on the machine actually running the map,
 * and `ui-tests/` scrapes exactly these numbers to assert them.
 */
export default function StatusBar({ connection, sceneStats, storeStats, topology, lodActive }) {
  const quality = sceneStats.fps >= 55 ? "good" : sceneStats.fps >= 30 ? "fair" : "poor";
  return (
    <div className="statusbar" role="status" aria-live="off">
      <span className="statusbar-item">
        <span className={`dot dot-${connection.state}`} aria-hidden="true" />
        <strong>{connection.state}</strong>
        {connection.attempt > 0 && connection.state !== "open" ? ` retry ${connection.attempt}` : ""}
      </span>
      <span className="statusbar-sep" aria-hidden="true">/</span>
      <span className={`statusbar-item fps-${quality}`} title="Rendered frames per second">
        {sceneStats.fps.toFixed(0)} fps
      </span>
      <span className="statusbar-item" title="WebGL draw calls in the last frame">
        {sceneStats.drawCalls} draws
      </span>
      <span className="statusbar-item" title="Nodes drawn individually versus total">
        {sceneStats.nodesRendered}/{topology?.nodes.length ?? 0} nodes
      </span>
      {sceneStats.animated > 0 && (
        <span className="statusbar-item" title="Nodes animating; the rest are marked but static">
          {sceneStats.animated} animated
          {sceneStats.suppressed > 0 ? ` · ${sceneStats.suppressed} static` : ""}
        </span>
      )}
      {lodActive && (
        <span className="statusbar-item badge" title="Subnets collapsed into clusters">
          LOD {sceneStats.clusters}
        </span>
      )}
      {sceneStats.quality && sceneStats.quality !== "high" && (
        <span className="statusbar-item badge" title="Effects reduced to protect the frame rate">
          {sceneStats.quality}
        </span>
      )}

      <span className="statusbar-spacer" />
      <span className="statusbar-item" title="Alerts received this session">
        <Icon name="activity" size={10} /> {storeStats.alerts.toLocaleString()}
      </span>
      <span className="statusbar-item" title="Time since the last state update">
        <Icon name="clock" size={10} />
        {storeStats.sinceLastUpdate === null ? "idle" : `${storeStats.sinceLastUpdate.toFixed(0)}ms`}
      </span>
      <span className="statusbar-sep" aria-hidden="true">/</span>
      <span className="statusbar-item faint">⌘K commands</span>
    </div>
  );
}
