"""Detection metrics for a completed benchmark run."""
from __future__ import annotations

import numpy as np


def detection_metrics(y: np.ndarray, scores: np.ndarray, threshold: float) -> dict:
    from sklearn.metrics import average_precision_score, roc_auc_score, roc_curve

    y = np.asarray(y).astype(np.int8)
    scores = np.asarray(scores, dtype=np.float64)
    pred = (scores >= threshold).astype(np.int8)
    tp = int(((pred == 1) & (y == 1)).sum())
    fp = int(((pred == 1) & (y == 0)).sum())
    fn = int(((pred == 0) & (y == 1)).sum())
    tn = int(((pred == 0) & (y == 0)).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    out = {
        "n": int(len(y)),
        "n_attack": int(y.sum()),
        "threshold": float(threshold),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "detection_rate": recall,
        "recall": recall,
        "precision": precision,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "fpr": fp / (fp + tn) if fp + tn else 0.0,
        "accuracy": (tp + tn) / max(len(y), 1),
    }
    if len(np.unique(y)) > 1:
        out["roc_auc"] = float(roc_auc_score(y, scores))
        out["pr_auc"] = float(average_precision_score(y, scores))
        fpr, tpr, thr = roc_curve(y, scores)
        out["roc_curve"] = {
            "fpr": [float(v) for v in fpr[:: max(len(fpr) // 200, 1)]],
            "tpr": [float(v) for v in tpr[:: max(len(tpr) // 200, 1)]],
        }
        out["detection_at_fpr"] = {}
        for budget in (0.001, 0.005, 0.01, 0.02):
            i = max(int(np.searchsorted(fpr, budget, side="right")) - 1, 0)
            out["detection_at_fpr"][f"{budget:g}"] = {
                "detection_rate": float(tpr[i]), "threshold": float(thr[i]),
            }
    else:
        out["roc_auc"] = float("nan")
        out["pr_auc"] = float("nan")
    return out


def latency_stats(latencies: np.ndarray, warmup_frac: float = 0.01) -> dict:
    """Latency percentiles, split into warm-up and steady state.

    ``latencies`` is in arrival order.  The split exists because the first
    batches of a run pay for cold ONNX sessions and cold numpy kernels, and
    reporting one ``max`` over the whole run hides whether a large outlier was
    a start-up artefact or something that recurs under load.
    """
    if latencies is None or len(latencies) == 0:
        return {}
    ms = np.asarray(latencies, dtype=np.float64) * 1000.0
    ms = ms[np.isfinite(ms) & (ms >= 0)]
    if not len(ms):
        return {}
    cut = max(int(len(ms) * warmup_frac), 1)
    warm, steady = ms[:cut], ms[cut:]
    out = {
        "count": int(len(ms)),
        "mean_ms": float(ms.mean()),
        "p50_ms": float(np.percentile(ms, 50)),
        "p90_ms": float(np.percentile(ms, 90)),
        "p99_ms": float(np.percentile(ms, 99)),
        "max_ms": float(ms.max()),
        "warmup_frac": warmup_frac,
        "warmup_max_ms": float(warm.max()),
    }
    if len(steady):
        out.update({
            "steady_p99_ms": float(np.percentile(steady, 99)),
            "steady_max_ms": float(steady.max()),
        })
    return out


def per_class(class_ids: np.ndarray, flagged: np.ndarray, class_names) -> dict:
    out = {}
    for cid in np.unique(class_ids):
        if cid < 0:
            continue
        mask = class_ids == cid
        name = class_names[cid] if cid < len(class_names) else str(cid)
        out[name] = {
            "count": int(mask.sum()),
            "flagged": int(flagged[mask].sum()),
            "flagged_rate": float(flagged[mask].mean()),
            "is_attack": name.startswith("attack_"),
        }
    return out


def format_report(report: dict) -> str:
    """Human-readable summary printed at the end of a bench run."""
    d, t = report["detection"], report["throughput"]
    lat = report.get("latency", {})
    lines = [
        "",
        "=" * 72,
        "  CyberNexus Stage 1 - benchmark report",
        "=" * 72,
        f"  bus                 {report['config']['bus']}   "
        f"ingest x{report['config']['ingest_workers']}  "
        f"scorer x{report['config']['scorer_workers']}  "
        f"gen x{report['config']['gen_workers']}",
        f"  model               {report['model']['name']} "
        f"({report['model']['backend']}), threshold {d['threshold']:.4f}",
        "",
        "  Throughput",
        f"    offered           {t['offered_rate']:>12,.0f} conn/s "
        f"(target {t['target_rate']:,.0f})",
        f"    sustained         {t['sustained_rate']:>12,.0f} conn/s end-to-end",
        f"    scored            {t['scored']:>12,} connections in {t['scoring_window_s']:.2f}s",
        f"    max backlog       {t['max_backlog']:>12,} entries "
        f"({'bounded' if t['backlog_bounded'] else 'GROWING - pipeline is behind'})",
        "",
        "  Detection",
        f"    detection rate    {d['detection_rate']:>12.4f}  "
        f"({d['tp']:,}/{d['tp'] + d['fn']:,} attacks caught)",
        f"    precision         {d['precision']:>12.4f}",
        f"    F1                {d['f1']:>12.4f}",
        f"    false positive    {d['fpr']:>12.4f}  ({d['fp']:,} of {d['fp'] + d['tn']:,} benign)",
        f"    ROC AUC           {d['roc_auc']:>12.5f}",
        f"    PR AUC            {d['pr_auc']:>12.5f}",
    ]
    if lat:
        lines += [
            "",
            "  End-to-end latency (emit -> verdict)",
            f"    p50 {lat['p50_ms']:.1f} ms   p90 {lat['p90_ms']:.1f} ms   "
            f"p99 {lat['p99_ms']:.1f} ms   max {lat['max_ms']:.1f} ms",
        ]
        if "steady_max_ms" in lat:
            lines.append(
                f"    after the first {lat['warmup_frac']:.0%} of connections: "
                f"p99 {lat['steady_p99_ms']:.1f} ms   max {lat['steady_max_ms']:.1f} ms "
                f"(warm-up max {lat['warmup_max_ms']:.1f} ms)"
            )
    if report.get("per_class"):
        lines += ["", "  Per-class flag rate"]
        for name, row in sorted(report["per_class"].items(),
                                key=lambda kv: (not kv[1]["is_attack"], kv[0])):
            kind = "attack" if row["is_attack"] else "benign"
            lines.append(f"    {name:<26} {kind:<7} {row['flagged_rate']:>7.3f}  "
                         f"({row['flagged']:,}/{row['count']:,})")
    crit = report["acceptance"]
    lines += [
        "",
        "  Acceptance criteria",
        f"    detection >= {crit['detection_target']:.0%}      "
        f"{'PASS' if crit['detection_pass'] else 'FAIL'}  ({d['detection_rate']:.4f})",
        f"    throughput >= {crit['throughput_target']:,.0f}/s  "
        f"{'PASS' if crit['throughput_pass'] else 'FAIL'}  ({t['sustained_rate']:,.0f}/s)",
        "=" * 72,
        "",
    ]
    return "\n".join(lines)
