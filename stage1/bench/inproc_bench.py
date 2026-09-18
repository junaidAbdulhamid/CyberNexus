"""Single-process benchmark: no Redis, no subprocesses, no privileges.

Runs generator -> in-memory bus -> ingest -> scorer on threads inside one
process.  It exists for two reasons: it is the CI smoke test for the whole
pipeline, and it isolates the *compute* cost of the pipeline from the bus, so
the gap between this number and ``run_bench`` is exactly what Redis and process
boundaries cost.

    python -m bench.inproc_bench --total 200000
"""
from __future__ import annotations

import argparse
import json
import threading
import time

import numpy as np

from bench.evaluate import detection_metrics, latency_stats
from cybernexus.bus import MemoryBus
from cybernexus.config import Settings
from cybernexus.logging_setup import setup_logging
from cybernexus.trafficmodel import DEFAULT_HARDNESS, TrafficModel
from cybernexus.records import pack_batch
from cybernexus.wire import unpack_scores
from ingest.consumer import FeatureStage
from model.scorer import ScoreStage
from cybernexus.service import StageRunner


def run(args) -> dict:
    setup_logging(args.log_level)
    settings = Settings()
    settings.bus = "memory"
    settings.emit_scores = True
    settings.publish_batch = args.batch
    settings.block_ms = 100
    bus = MemoryBus(maxlen=1_000_000)

    ingest_stage = FeatureStage(bus, settings)
    score_stage = ScoreStage(bus, settings, model_dir=args.model_dir,
                             backend=args.backend, emit_scores=True)

    ingest_runner = StageRunner("ingest", bus, settings, settings.conn_stream,
                                settings.ingest_group, ingest_stage.handle, idle_exit_s=2.0)
    score_runner = StageRunner("scorer", bus, settings, settings.feature_stream,
                               settings.scorer_group, score_stage.handle, idle_exit_s=3.0)
    threads = [
        threading.Thread(target=ingest_runner.run, name="ingest", daemon=True),
        threading.Thread(target=score_runner.run, name="scorer", daemon=True),
    ]
    for t in threads:
        t.start()

    model = TrafficModel(seed=args.seed, hardness=args.hardness, drift=args.drift)
    t0 = time.perf_counter()
    produced = 0
    while produced < args.total:
        take = min(args.batch, args.total - produced)
        recs, _ = model.sample_records(take)
        bus.publish(settings.conn_stream, [pack_batch(recs)])
        produced += take
    t_gen = time.perf_counter() - t0

    for t in threads:
        t.join(timeout=args.timeout)
    t_total = time.perf_counter() - t0

    ids, scores, labels, latency = [], [], [], []
    bus.ensure_group(settings.score_stream, "bench")
    while True:
        batch = bus.consume(settings.score_stream, "bench", "c", 256, 50)
        if not batch:
            break
        for _mid, blob in batch:
            d = unpack_scores(blob)
            ids.extend(d["ids"])
            scores.append(d["s"])
            if d["y"] is not None:
                labels.append(d["y"])
            if d["l"] is not None:
                latency.append(d["l"])
    scores = np.concatenate(scores) if scores else np.empty(0)
    labels = np.concatenate(labels) if labels else np.empty(0)
    latency = np.concatenate(latency) if latency else np.empty(0)

    detection = detection_metrics(labels, scores, score_stage.engine.threshold)
    detection.pop("roc_curve", None)
    out = {
        "records": produced,
        "scored": int(len(scores)),
        "generate_rate": produced / max(t_gen, 1e-9),
        # Measured over the window the scorer was busy; the wall-clock figure
        # would otherwise include the idle-exit wait at the end of the run.
        "pipeline_rate": score_runner.active_rate(),
        "ingest_rate": ingest_runner.active_rate(),
        "wallclock_rate": len(scores) / max(t_total, 1e-9),
        "detection": {k: detection[k] for k in
                      ("detection_rate", "precision", "f1", "fpr", "roc_auc", "n", "n_attack")},
        "latency": latency_stats(latency),
        "backend": score_stage.engine.backend,
    }
    print(json.dumps(out, indent=2, default=float))
    return out


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Single-process pipeline benchmark (no Redis)")
    p.add_argument("--total", type=int, default=200_000)
    p.add_argument("--batch", type=int, default=512)
    p.add_argument("--seed", type=int, default=99991)
    p.add_argument("--drift", type=float, default=0.0)
    p.add_argument("--hardness", type=float, default=DEFAULT_HARDNESS)
    p.add_argument("--model-dir", default=None)
    p.add_argument("--backend", default=None, choices=["auto", "onnx", "joblib"])
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--log-level", default="WARNING")
    return p.parse_args(argv)


if __name__ == "__main__":
    run(parse_args())
