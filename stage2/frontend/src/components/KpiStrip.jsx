import React, { useEffect, useMemo, useRef, useState } from "react";
import Icon from "./Icon.jsx";
import Sparkline from "./Sparkline.jsx";
import { SEVERITIES, SEVERITY_RANK } from "../lib/theme.js";

/**
 * The status band across the top of the map.
 *
 * Chosen to answer, at a glance and in this order: *is anything wrong right
 * now*, *how bad*, *how much is arriving*, *is the pipeline healthy*, *is the
 * picture current*. Everything else an operator might want is one click away;
 * these five are the ones worth permanent screen space.
 *
 * Numbers animate to their new value rather than jumping. A counter that snaps
 * is easy to miss in peripheral vision; one that moves is not.
 */
export default function KpiStrip({ states, topology, stats, connection, sceneStats }) {
  const ranked = states.ranked();
  const worst = ranked[0] || null;
  const alertsPerMin = useAlertRate(states.counters.alerts);
  const history = useHistory(ranked.length, 40);

  const bySeverity = useMemo(() => {
    const counts = Object.fromEntries(SEVERITIES.map((s) => [s, 0]));
    for (const state of ranked) counts[state.severity] += 1;
    return counts;
  }, [ranked]);

  const threatRank = worst ? SEVERITY_RANK[worst.severity] : 0;
  const threatLabel = ["clear", "info", "low", "medium", "high", "critical"][threatRank];
  const coverage = topology ? topology.nodes.length : 0;
  const quiet = coverage - ranked.length;

  return (
    <div className="kpi-strip" role="group" aria-label="Network status summary">
      <div className={`kpi ${threatRank >= 5 ? "is-critical" : threatRank >= 4 ? "is-high" : threatRank === 0 ? "is-ok" : ""}`}>
        <span className="kpi-label"><Icon name="shield" size={11} /> Threat level</span>
        <span className="kpi-value" data-testid="kpi-threat">
          {threatLabel.toUpperCase()}
        </span>
        <ThreatMeter rank={threatRank} />
      </div>

      <div className={`kpi ${ranked.length > 0 ? "is-high" : "is-ok"}`}>
        <span className="kpi-label"><Icon name="alert" size={11} /> Hosts alerting</span>
        <span className="kpi-value" data-testid="kpi-alerting">
          <AnimatedNumber value={ranked.length} />
          <span className="kpi-unit">/ {coverage}</span>
        </span>
        <span className="kpi-foot">
          {bySeverity.critical > 0 && <span className="sev-text sev-critical">{bySeverity.critical} critical · </span>}
          {quiet} quiet
        </span>
        <Sparkline values={history} color="var(--sev-high)" />
      </div>

      <div className="kpi">
        <span className="kpi-label"><Icon name="activity" size={11} /> Alert rate</span>
        <span className="kpi-value">
          {alertsPerMin === null
            ? <span className="kpi-unit" style={{ fontSize: 13 }}>measuring…</span>
            : <><AnimatedNumber value={alertsPerMin} /><span className="kpi-unit">/min</span></>}
        </span>
        <span className="kpi-foot">{states.counters.alerts.toLocaleString()} this session</span>
      </div>

      <div className="kpi">
        <span className="kpi-label"><Icon name="network" size={11} /> Monitored</span>
        <span className="kpi-value">
          <AnimatedNumber value={coverage} />
          <span className="kpi-unit">devices</span>
        </span>
        <span className="kpi-foot">
          {topology ? `${topology.links.length} links · ${Object.keys(topology.stats?.by_subnet || {}).length} subnets` : "—"}
        </span>
      </div>

      <div className={`kpi ${connection.state === "open" ? "is-ok" : "is-high"}`}>
        <span className="kpi-label"><Icon name="pulse" size={11} /> Pipeline</span>
        <span className="kpi-value" style={{ fontSize: 15 }}>
          {connection.state === "open" ? "STREAMING" : connection.state.toUpperCase()}
        </span>
        <span className="kpi-foot">
          {sceneStats.fps ? `${sceneStats.fps.toFixed(0)} fps · ` : ""}
          {stats?.total_alerts != null ? `${stats.total_alerts.toLocaleString()} ingested` : "live"}
        </span>
      </div>
    </div>
  );
}

function ThreatMeter({ rank }) {
  const colors = ["--sev-none", "--sev-info", "--sev-low", "--sev-medium", "--sev-high", "--sev-critical"];
  return (
    <div className="threat-meter" aria-hidden="true">
      {[1, 2, 3, 4, 5].map((level) => (
        <span
          key={level}
          className={`threat-bar ${rank >= level ? "is-lit" : ""}`}
          style={{
            height: `${6 + level * 3.5}px`,
            background: rank >= level ? `var(${colors[rank]})` : undefined,
            color: rank >= level ? `var(${colors[rank]})` : undefined,
          }}
        />
      ))}
    </div>
  );
}

/** Eases a number toward its target so changes are noticed, not missed. */
function AnimatedNumber({ value, duration = 420 }) {
  const [shown, setShown] = useState(value);
  const fromRef = useRef(value);
  const startRef = useRef(0);
  const frameRef = useRef(0);

  useEffect(() => {
    if (value === shown) return;
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (reduced) { setShown(value); return; }
    fromRef.current = shown;
    startRef.current = performance.now();
    const tick = (now) => {
      const t = Math.min((now - startRef.current) / duration, 1);
      const eased = 1 - (1 - t) ** 3;
      setShown(Math.round(fromRef.current + (value - fromRef.current) * eased));
      if (t < 1) frameRef.current = requestAnimationFrame(tick);
    };
    frameRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frameRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  return <>{shown.toLocaleString()}</>;
}

/**
 * Alerts per minute, from the delta between samples.
 *
 * The window has to have actually elapsed before the rate means anything. An
 * earlier version divided by whatever tiny interval had passed since mount and
 * cheerfully reported four-figure rates from a few dozen alerts.
 */
function useAlertRate(total, windowMs = 15000, minElapsedMs = 4000) {
  const samples = useRef([]);
  const [rate, setRate] = useState(null);

  useEffect(() => {
    const now = performance.now();
    samples.current.push({ now, total });
    while (samples.current.length > 2 && now - samples.current[0].now > windowMs) {
      samples.current.shift();
    }
    const first = samples.current[0];
    const elapsed = now - first.now;
    if (elapsed >= minElapsedMs) {
      setRate(Math.max(0, Math.round(((total - first.total) / (elapsed / 1000)) * 60)));
    }
  }, [total, windowMs, minElapsedMs]);

  return rate;
}

/** A rolling series for the sparkline. */
function useHistory(value, length) {
  const [series, setSeries] = useState(() => new Array(length).fill(0));
  const latest = useRef(value);
  latest.current = value;
  useEffect(() => {
    const id = setInterval(() => {
      setSeries((prev) => [...prev.slice(1), latest.current]);
    }, 1500);
    return () => clearInterval(id);
  }, []);
  return series;
}
