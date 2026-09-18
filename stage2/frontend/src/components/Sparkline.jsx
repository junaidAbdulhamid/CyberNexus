import React, { useId } from "react";

/**
 * A sparkline sized to its container.
 *
 * Deliberately axis-free and label-free: at this size the only readable
 * information is the *shape* — is it rising, is it spiky, is it flat — and
 * adding ticks would cost pixels without adding meaning. The precise number
 * lives next to it in the KPI tile.
 */
export default function Sparkline({ values = [], color = "currentColor", height = 26, fill = true }) {
  const gradientId = useId();
  if (values.length < 2) return <svg className="kpi-spark" height={height} aria-hidden="true" />;

  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const step = 100 / (values.length - 1);
  const points = values.map((value, i) => {
    const x = i * step;
    const y = 100 - ((value - min) / span) * 100;
    return [x, y];
  });

  const line = points.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
  const area = `${line} L100,100 L0,100 Z`;

  return (
    <svg
      className="kpi-spark" viewBox="0 0 100 100" preserveAspectRatio="none"
      height={height} aria-hidden="true"
    >
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.45" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      {fill && <path d={area} fill={`url(#${gradientId})`} />}
      <path d={line} fill="none" stroke={color} strokeWidth="2" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
