import React, { useEffect, useState } from "react";
import Icon from "./Icon.jsx";

/**
 * Cold-start overlay.
 *
 * It exists because the honest alternative is worse: loading a topology,
 * opening a WebSocket and compiling shaders takes a second or two, and an empty
 * black canvas during that time reads as "broken". The steps shown are the real
 * ones and they tick as they actually complete — this is a progress indicator,
 * not a loading-themed animation played over a fixed delay.
 */
const STEPS = [
  { key: "topology", label: "loading topology" },
  { key: "scene", label: "initialising renderer" },
  { key: "stream", label: "connecting alert stream" },
  { key: "ready", label: "ready" },
];

export default function BootSequence({ progress, error = null, onDone }) {
  const [leaving, setLeaving] = useState(false);

  const doneCount = STEPS.filter((step) => progress[step.key]).length;
  const complete = doneCount === STEPS.length;

  useEffect(() => {
    if (!complete || error) return;
    const hold = setTimeout(() => setLeaving(true), 260);
    const finish = setTimeout(onDone, 640);
    return () => { clearTimeout(hold); clearTimeout(finish); };
  }, [complete, error, onDone]);

  return (
    <div className={`boot ${leaving ? "is-leaving" : ""}`} role="status" aria-live="polite">
      <div className="boot-mark"><Icon name="shield" size={26} strokeWidth={1.9} /></div>
      <div className="boot-title">CyberNexus</div>
      <div className="boot-bar">
        <span style={{ width: `${(doneCount / STEPS.length) * 100}%` }} />
      </div>
      <div className="boot-steps">
        {STEPS.map((step) => (
          <div key={step.key} className={`boot-step ${progress[step.key] ? "is-done" : ""}`}>
            <span className="boot-step-tick">{progress[step.key] ? "✓" : "·"}</span>
            {step.label}
          </div>
        ))}
      </div>
      {error && (
        // Failing visibly beats a progress bar that never finishes.
        <p className="boot-error" role="alert">{error}</p>
      )}
    </div>
  );
}
