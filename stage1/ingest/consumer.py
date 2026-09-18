"""Ingest stage: connection records -> feature vectors.

Reads batches of connection records from the ``cn:conns`` stream as a consumer
group, optionally folds partial records of the same flow back together, runs the
vectorised extractor, and publishes float32 feature blocks to ``cn:features``.

Two deployment shapes, both here:

``--workers N``   N processes in the same consumer group.  Redis hands each
                  batch to exactly one of them, so scaling out is just raising
                  N — no partitioning scheme to maintain.
``--fused``       Skip the feature stream and score inline in this process.
                  One less hop and one less serialisation round trip, which is
                  what a single-box deployment should run; the split default is
                  what lets feature extraction and inference scale separately.
"""
from __future__ import annotations

import argparse
import logging
import multiprocessing as mp
from typing import Sequence

from cybernexus.bus import make_bus
from cybernexus.config import Settings, settings as default_settings
from cybernexus.features import extract_matrix, numeric_matrix
from cybernexus.flowtable import merge_records
from cybernexus.logging_setup import setup_logging
from cybernexus.records import FIELD_INDEX, NUM_INDEX
from cybernexus.service import StageRunner
from cybernexus.wire import pack_features
from cybernexus import records as records_mod

log = logging.getLogger("ingest")


class FeatureStage:
    """Stateless per-batch transform: records in, feature block out."""

    def __init__(self, bus, settings: Settings, aggregate: bool = False, scorer=None):
        self.bus = bus
        self.settings = settings
        self.aggregate = aggregate
        self.scorer = scorer  # set in --fused mode

    def handle(self, payloads: Sequence[bytes]) -> int:
        recs: list = []
        for blob in payloads:
            recs.extend(records_mod.unpack_batch(blob))
        if not recs:
            return 0
        if self.aggregate:
            recs = list(merge_records(recs))

        mat = numeric_matrix(recs)
        X = extract_matrix(mat)
        fi = FIELD_INDEX
        ids = [r[fi["flow_id"]] for r in recs]
        meta = [
            (r[fi["src_ip"]], r[fi["dst_ip"]], r[fi["src_port"]], r[fi["dst_port"]], r[fi["protocol"]])
            for r in recs
        ]
        labels = mat[:, NUM_INDEX["label"]]
        emit_ts = mat[:, NUM_INDEX["emit_ts"]]

        if self.scorer is not None:
            return self.scorer.process(ids, meta, X, labels, emit_ts)

        self.bus.publish(
            self.settings.feature_stream, [pack_features(ids, meta, X, labels, emit_ts)]
        )
        return len(recs)


def run_worker(worker_index: int, args, settings: Settings) -> int:
    setup_logging(settings.log_level)
    bus = make_bus(settings.bus, settings.redis_url, settings.stream_maxlen)
    scorer = None
    if args.fused:
        from model.scorer import ScoreStage

        scorer = ScoreStage(bus, settings, model_dir=args.model_dir, backend=args.backend)
    stage = FeatureStage(bus, settings, aggregate=args.aggregate, scorer=scorer)
    runner = StageRunner(
        name="ingest",
        bus=bus,
        settings=settings,
        in_stream=settings.conn_stream,
        group=settings.ingest_group,
        handler=stage.handle,
        worker_index=worker_index,
        metrics_port=args.metrics_port,
        idle_exit_s=args.idle_exit,
    )
    total = runner.run(max_seconds=args.max_seconds)
    bus.close()
    return total


def main(argv=None) -> None:
    args = parse_args(argv)
    settings = Settings()
    if args.bus:
        settings.bus = args.bus
    if args.workers > 1 and settings.bus != "redis":
        log.warning("multi-worker mode needs a shared bus; forcing --workers 1 on %s",
                    settings.bus)
        args.workers = 1
    setup_logging(settings.log_level)

    if args.workers == 1:
        run_worker(0, args, settings)
        return
    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=run_worker, args=(i, args, settings), name=f"ingest-{i}", daemon=False)
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
    p = argparse.ArgumentParser(description="CyberNexus ingest / feature extraction stage")
    p.add_argument("--workers", type=int, default=default_settings.ingest_workers)
    p.add_argument("--bus", default=None, choices=["redis", "memory"])
    p.add_argument("--aggregate", action="store_true",
                   help="merge partial records that share a flow_id before extraction")
    p.add_argument("--fused", action="store_true",
                   help="score in-process instead of publishing to the feature stream")
    p.add_argument("--model-dir", default=None, help="model directory for --fused")
    p.add_argument("--backend", default=None, choices=["auto", "onnx", "joblib"])
    p.add_argument("--metrics-port", type=int, default=0)
    p.add_argument("--max-seconds", type=float, default=0.0)
    p.add_argument("--idle-exit", type=float, default=0.0,
                   help="exit after N seconds with no input (used by the bench harness)")
    return p.parse_args(argv)


if __name__ == "__main__":
    main()
