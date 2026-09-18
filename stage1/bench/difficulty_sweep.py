"""How detection degrades as the simulated traffic gets harder.

A single headline detection rate against a simulator is easy to manufacture:
turn down the class overlap and any model scores 100%.  This sweep is the
antidote.  It holds the trained model fixed and evaluates it against traffic of
increasing ambiguity (``hardness``) and increasing distribution shift
(``drift``), so the report shows the shape of the degradation rather than one
flattering point on it.

    python -m bench.difficulty_sweep --n 200000
"""
from __future__ import annotations

import argparse
import json

import numpy as np

from bench.evaluate import detection_metrics
from cybernexus.logging_setup import setup_logging
from cybernexus.trafficmodel import DEFAULT_HARDNESS
from model.dataset import build_synthetic
from model.infer import InferenceEngine


def sweep(args) -> dict:
    setup_logging(args.log_level)
    engine = InferenceEngine(model_dir=args.model_dir, backend=args.backend)
    rows = []
    for hardness in args.hardness:
        for drift in args.drift:
            ds = build_synthetic(args.n, seed=args.seed, drift=drift)
            # build_synthetic uses the model default hardness; re-sample when a
            # different one is requested.
            if hardness != DEFAULT_HARDNESS:
                from cybernexus.features import extract_matrix
                from cybernexus.records import NUM_INDEX
                from cybernexus.trafficmodel import TrafficModel

                model = TrafficModel(seed=args.seed, drift=drift, hardness=hardness)
                mat, _ = model.sample_matrix(args.n)
                X = extract_matrix(mat)
                y = mat[:, NUM_INDEX["label"]].astype(np.int8)
            else:
                X, y = ds.X, ds.y
            scores = engine.score(X)
            m = detection_metrics(y, scores, engine.threshold)
            rows.append({
                "hardness": hardness,
                "drift": drift,
                "detection_rate": m["detection_rate"],
                "precision": m["precision"],
                "f1": m["f1"],
                "fpr": m["fpr"],
                "roc_auc": m["roc_auc"],
                "detection_at_fpr_1pct": m["detection_at_fpr"]["0.01"]["detection_rate"],
            })
            print(f"hardness={hardness:<6} drift={drift:<5} "
                  f"detection={m['detection_rate']:.4f} fpr={m['fpr']:.4f} "
                  f"f1={m['f1']:.4f} auc={m['roc_auc']:.5f} "
                  f"det@1%fpr={rows[-1]['detection_at_fpr_1pct']:.4f}")

    out = {"n": args.n, "threshold": engine.threshold, "backend": engine.backend, "rows": rows}
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(out, fh, indent=2, default=float)
        print(f"\nwrote {args.out}")
    print("\n| hardness | drift | detection | FPR | F1 | ROC AUC | detection @1% FPR |")
    print("|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        print(f"| {r['hardness']} | {r['drift']} | {r['detection_rate']:.4f} | "
              f"{r['fpr']:.4f} | {r['f1']:.4f} | {r['roc_auc']:.5f} | "
              f"{r['detection_at_fpr_1pct']:.4f} |")
    return out


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Detection vs simulated-traffic difficulty")
    p.add_argument("--n", type=int, default=200_000)
    p.add_argument("--hardness", type=float, nargs="+",
                   default=[0.0, 0.025, DEFAULT_HARDNESS, 0.10, 0.15])
    p.add_argument("--drift", type=float, nargs="+", default=[0.0])
    p.add_argument("--seed", type=int, default=99991)
    p.add_argument("--model-dir", default=None)
    p.add_argument("--backend", default=None, choices=["auto", "onnx", "joblib"])
    p.add_argument("--out", default=None)
    p.add_argument("--log-level", default="WARNING")
    return p.parse_args(argv)


if __name__ == "__main__":
    sweep(parse_args())
