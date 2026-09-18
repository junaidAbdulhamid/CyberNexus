"""Scoring stage: feature vectors -> alerts.

Reads feature blocks from ``cn:features``, runs one inference call per batch,
and publishes the rows above threshold to ``cn:alerts``.  With
``--emit-scores`` it also publishes *every* score to ``cn:scores``; the bench
harness needs the full score vector to compute a ROC curve, production does not
and leaves it off.

Everything about the model lives behind :class:`model.infer.InferenceEngine`,
so this file never changes when the model does.
"""
from __future__ import annotations

import argparse
import logging
import multiprocessing as mp
import time
from typing import Sequence

import numpy as np

from cybernexus.bus import make_bus
from cybernexus.config import Settings, settings as default_settings
from cybernexus.logging_setup import setup_logging
from cybernexus.metrics import ALERTS, E2E
from cybernexus.service import StageRunner
from cybernexus.wire import pack_alerts, pack_scores, unpack_features
from model.infer import InferenceEngine

log = logging.getLogger("scorer")


class ScoreStage:
    def __init__(self, bus, settings: Settings, model_dir=None, backend=None,
                 threshold: float | None = None, emit_scores: bool | None = None):
        self.bus = bus
        self.settings = settings
        self.engine = InferenceEngine(model_dir=model_dir, backend=backend, threshold=threshold)
        self.emit_scores = settings.emit_scores if emit_scores is None else emit_scores
        self.alerts = 0

    def process(self, ids, meta, X: np.ndarray, labels, emit_ts) -> int:
        now = time.time()
        scores, flagged = self.engine.predict(X)
        latency = (now - emit_ts).astype(np.float32) if emit_ts is not None else None

        idx = np.flatnonzero(flagged)
        if idx.size:
            alerts = []
            for i in idx:
                src_ip, dst_ip, sport, dport, proto = meta[i]
                lat = float(latency[i]) if latency is not None else 0.0
                alerts.append({
                    "flow_id": ids[i],
                    "src_ip": src_ip,
                    "dst_ip": dst_ip,
                    "src_port": int(sport),
                    "dst_port": int(dport),
                    "protocol": int(proto),
                    "score": float(scores[i]),
                    "threshold": float(self.engine.threshold),
                    "detected_at": now,
                    "latency_s": lat,
                    "label": float(labels[i]) if labels is not None else -1.0,
                })
            self.bus.publish(self.settings.alert_stream, [pack_alerts(alerts)])
            self.alerts += len(alerts)
            ALERTS.labels("scorer").inc(len(alerts))

        if self.emit_scores:
            self.bus.publish(
                self.settings.score_stream, [pack_scores(ids, scores, labels, latency)]
            )
        if latency is not None and latency.size:
            E2E.labels("scorer").observe(float(latency.mean()))
        return len(ids)

    def handle(self, payloads: Sequence[bytes]) -> int:
        total = 0
        for blob in payloads:
            d = unpack_features(blob)
            total += self.process(d["ids"], d["meta"], d["x"], d["y"], d["t"])
        return total


def run_worker(worker_index: int, args, settings: Settings) -> int:
    setup_logging(settings.log_level)
    bus = make_bus(settings.bus, settings.redis_url, settings.stream_maxlen)
    stage = ScoreStage(
        bus, settings, model_dir=args.model_dir, backend=args.backend,
        threshold=args.threshold, emit_scores=args.emit_scores or None,
    )
    runner = StageRunner(
        name="scorer",
        bus=bus,
        settings=settings,
        in_stream=settings.feature_stream,
        group=settings.scorer_group,
        handler=stage.handle,
        worker_index=worker_index,
        metrics_port=args.metrics_port,
        idle_exit_s=args.idle_exit,
    )
    total = runner.run(max_seconds=args.max_seconds)
    log.info("scorer[%d] raised %d alerts", worker_index, stage.alerts)
    bus.close()
    return total


def main(argv=None) -> None:
    args = parse_args(argv)
    settings = Settings()
    if args.bus:
        settings.bus = args.bus
    if args.emit_scores:
        settings.emit_scores = True
    if args.workers > 1 and settings.bus != "redis":
        args.workers = 1
    setup_logging(settings.log_level)
    if args.workers == 1:
        run_worker(0, args, settings)
        return
    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=run_worker, args=(i, args, settings), name=f"scorer-{i}")
        for i in range(args.workers)
    ]
    for p in procs:
        p.start()
    try:
        for p in procs:
            p.join()
    except KeyboardInterrupt:  # pragma: no cover
        for p in procs:
            p.terminate()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="CyberNexus inference / alerting stage")
    p.add_argument("--workers", type=int, default=default_settings.scorer_workers)
    p.add_argument("--bus", default=None, choices=["redis", "memory"])
    p.add_argument("--model-dir", default=None)
    p.add_argument("--backend", default=None, choices=["auto", "onnx", "joblib"])
    p.add_argument("--threshold", type=float, default=None)
    p.add_argument("--emit-scores", action="store_true",
                   help="publish every score (bench mode); off in production")
    p.add_argument("--metrics-port", type=int, default=0)
    p.add_argument("--max-seconds", type=float, default=0.0)
    p.add_argument("--idle-exit", type=float, default=0.0)
    return p.parse_args(argv)


if __name__ == "__main__":
    main()
