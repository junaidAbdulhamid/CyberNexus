import React, { useEffect, useMemo, useRef, useState } from "react";
import { SEVERITY_RANK } from "../lib/theme.js";

/**
 * History scrubber with playback.
 *
 * Two modes: **live**, where the map follows the stream, and **scrubbing**,
 * where the map shows reconstructed state at a chosen instant. Reconstruction
 * happens server-side (`/api/replay`) from the event log, so the client does not
 * have to keep every historical state in memory.
 *
 * The density strip above the slider is the useful part: it shows *when* things
 * happened, coloured by the worst severity in each bucket, so "what did the
 * last ten minutes look like" is answered before anyone drags anything.
 */
export default function Timeline({ api, onReplay, onLive, live }) {
  const [events, setEvents] = useState([]);
  const [range, setRange] = useState(null);
  const [value, setValue] = useState(1);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(4);
  const timerRef = useRef(null);

  const refresh = async () => {
    const data = await api.timeline({ limit: 5000 });
    setEvents(data.events || []);
    if (data.span) setRange(data.span);
  };

  useEffect(() => {
    refresh();
    const id = setInterval(() => { if (live) refresh(); }, 5000);
    return () => clearInterval(id);
  }, [api, live]);

  const buckets = useMemo(() => {
    if (!range || !events.length) return [];
    const count = 90;
    const span = Math.max(range.end - range.start, 1);
    const out = new Array(count).fill(null).map(() => ({ n: 0, worst: "none" }));
    for (const event of events) {
      const i = Math.min(Math.floor(((event.ts - range.start) / span) * count), count - 1);
      const bucket = out[Math.max(i, 0)];
      bucket.n += 1;
      if (SEVERITY_RANK[event.severity] > SEVERITY_RANK[bucket.worst]) bucket.worst = event.severity;
    }
    const peak = Math.max(...out.map((b) => b.n), 1);
    return out.map((b) => ({ ...b, height: b.n / peak }));
  }, [events, range]);

  useEffect(() => {
    if (!playing || !range) return;
    timerRef.current = setInterval(() => {
      setValue((current) => {
        const next = current + 0.004 * speed;
        if (next >= 1) { setPlaying(false); onLive(); return 1; }
        onReplay(range.start + (range.end - range.start) * next);
        return next;
      });
    }, 100);
    return () => clearInterval(timerRef.current);
  }, [playing, speed, range, onReplay, onLive]);

  const atTime = range ? range.start + (range.end - range.start) * value : null;

  const scrub = (next) => {
    setValue(next);
    setPlaying(false);
    if (!range) return;
    const ts = range.start + (range.end - range.start) * next;
    if (next >= 0.999) onLive(); else onReplay(ts);
  };

  return (
    <div className="timeline" role="group" aria-label="Alert history timeline">
      <div className="timeline-controls">
        <button
          type="button" className={`btn btn-small ${live ? "is-on" : ""}`}
          onClick={() => { setPlaying(false); setValue(1); onLive(); }}
          aria-pressed={live}
        >
          ● Live
        </button>
        <button
          type="button" className="btn btn-small" disabled={!range}
          onClick={() => setPlaying((p) => !p)}
          aria-label={playing ? "Pause playback" : "Play history"}
        >
          {playing ? "❚❚ Pause" : "▶ Play"}
        </button>
        <label className="speed-label">
          Speed
          <select value={speed} onChange={(e) => setSpeed(Number(e.target.value))} aria-label="Playback speed">
            <option value={1}>1×</option><option value={4}>4×</option>
            <option value={10}>10×</option><option value={30}>30×</option>
          </select>
        </label>
        <span className="timeline-readout" data-testid="timeline-readout">
          {live ? "following live stream"
                : atTime ? new Date(atTime * 1000).toLocaleTimeString() : "no history"}
        </span>
        <a className="btn btn-small" href={api.exportUrl("csv")} download>Export CSV</a>
        <a className="btn btn-small" href={api.exportUrl("json")} download>JSON</a>
      </div>

      <div className="timeline-density" aria-hidden="true">
        {buckets.map((bucket, i) => (
          <span
            key={i}
            className={`density-bar sev-bg-${bucket.worst}`}
            style={{ height: `${Math.max(bucket.height * 100, bucket.n ? 8 : 0)}%` }}
          />
        ))}
      </div>

      <input
        type="range" min={0} max={1} step={0.001} value={value}
        onChange={(e) => scrub(Number(e.target.value))}
        className="timeline-slider"
        aria-label="Scrub alert history"
        aria-valuetext={live ? "live" : atTime ? new Date(atTime * 1000).toLocaleTimeString() : "no data"}
        disabled={!range}
      />
    </div>
  );
}
