import React from "react";

/**
 * Connection state and the performance HUD.
 *
 * The FPS / draw-call / update-latency readout is not decoration: it is how the
 * brief's performance requirements are checked on the machine that is actually
 * running the map, and it is what `ui-tests/` scrapes to assert them.
 */
export default function StatusBar({ connection, sceneStats, storeStats, topology, lodActive }) {
  const quality = sceneStats.fps >= 55 ? "good" : sceneStats.fps >= 30 ? "fair" : "poor";
  return (
    <div className="statusbar" role="status" aria-live="off">
      <span className={`dot dot-${connection.state}`} aria-hidden="true" />
      <span className="statusbar-item">
        <strong>{connection.state}</strong>
        {connection.attempt > 0 && connection.state !== "open" ? ` (retry ${connection.attempt})` : ""}
      </span>
      <span className="statusbar-sep" aria-hidden="true">|</span>
      <span className="statusbar-item" title="Rendered frames per second">
        <span className={`fps fps-${quality}`}>{sceneStats.fps.toFixed(0)} fps</span>
      </span>
      <span className="statusbar-item" title="WebGL draw calls per frame">
        {sceneStats.drawCalls} draws
      </span>
      <span className="statusbar-item" title="Nodes currently rendered individually">
        {sceneStats.nodesRendered}/{topology?.nodes.length ?? 0} nodes
      </span>
      {lodActive && (
        <span className="statusbar-item badge" title="Subnets are collapsed into clusters">
          LOD: {sceneStats.clusters} clusters
        </span>
      )}
      <span className="statusbar-sep" aria-hidden="true">|</span>
      <span className="statusbar-item" title="Alerts received this session">
        {storeStats.alerts} alerts
      </span>
      <span className="statusbar-item" title="Time since the last state update">
        {storeStats.sinceLastUpdate === null ? "idle" : `${storeStats.sinceLastUpdate.toFixed(0)} ms ago`}
      </span>
    </div>
  );
}
