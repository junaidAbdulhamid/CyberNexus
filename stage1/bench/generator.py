"""Synthetic traffic generator.

Publishes labelled connection records onto the connection stream at a target
rate.  Labels ride along inside the record (field ``label``) and are ignored by
the feature extractor, so ground truth reaches the evaluation stage without any
side channel and without the model ever seeing it.

Rate control is a token bucket over whole batches: at 10k conn/s and a 512-flow
batch that is ~20 publishes per second, so the pacing error is bounded by one
batch rather than by the OS timer resolution.  ``--rate 0`` removes the limit
and measures how fast this box can actually go.
"""
from __future__ import annotations

import argparse
import logging
import multiprocessing as mp
import time
from pathlib import Path

import msgpack
import numpy as np

from cybernexus.bus import make_bus
from cybernexus.config import Settings
from cybernexus.logging_setup import setup_logging
from cybernexus.metrics import RateMeter
from cybernexus.records import pack_batch
from cybernexus.trafficmodel import DEFAULT_HARDNESS, TrafficModel

log = logging.getLogger("generator")


def write_truth(path: Path, ids: list[str], class_ids: np.ndarray) -> None:
    """Persist flow_id -> traffic class for the per-class bench breakdown."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(msgpack.packb({
        "ids": ids,
        "class_ids": np.asarray(class_ids, dtype=np.int16).tobytes(),
    }, use_bin_type=True))


def read_truth(path: Path) -> tuple[list[str], np.ndarray]:
    d = msgpack.unpackb(path.read_bytes(), raw=False, use_list=True)
    return d["ids"], np.frombuffer(d["class_ids"], dtype=np.int16)


def generate(
    worker_index: int,
    rate: float,
    duration: float,
    total: int,
    seed: int,
    drift: float,
    hardness: float,
    batch_size: int,
    settings: Settings,
    truth_path: str | None = None,
    quiet: bool = False,
) -> dict:
    setup_logging(settings.log_level)
    bus = make_bus(settings.bus, settings.redis_url, settings.stream_maxlen)
    model = TrafficModel(seed=seed + worker_index * 7919, drift=drift, hardness=hardness,
                         id_prefix=f"w{worker_index}-")
    meter = RateMeter()
    ids_out: list[str] = []
    classes_out: list[np.ndarray] = []
    collect_truth = truth_path is not None

    interval = batch_size / rate if rate > 0 else 0.0
    start = time.perf_counter()
    next_due = start
    deadline = start + duration if duration else float("inf")
    emitted = 0
    last_log = start

    while True:
        now = time.perf_counter()
        if now >= deadline or (total and emitted >= total):
            break
        take = batch_size if not total else min(batch_size, total - emitted)
        recs, class_ids = model.sample_records(take)
        blob = pack_batch(recs)
        bus.publish(settings.conn_stream, [blob])
        emitted += take
        meter.add(take)
        if collect_truth:
            ids_out.extend(r[0] for r in recs)
            classes_out.append(class_ids)

        if interval:
            next_due += interval
            sleep_for = next_due - time.perf_counter()
            if sleep_for > 0:
                time.sleep(sleep_for)
            elif sleep_for < -1.0:
                # More than a second behind: the box cannot hold this rate.
                # Re-baseline so the report shows the achieved rate rather than
                # an ever-growing debt.
                next_due = time.perf_counter()
        if not quiet and time.perf_counter() - last_log >= 5.0:
            last_log = time.perf_counter()
            log.info("gen[%d] %.0f conn/s (total %d)", worker_index, meter.rate(), emitted)

    elapsed = time.perf_counter() - start
    if collect_truth and ids_out:
        write_truth(Path(truth_path), ids_out, np.concatenate(classes_out))
    bus.close()
    result = {
        "worker": worker_index,
        "emitted": emitted,
        "elapsed_s": elapsed,
        "rate": emitted / max(elapsed, 1e-9),
    }
    log.info("gen[%d] done: %d records in %.2fs (%.0f conn/s)",
             worker_index, emitted, elapsed, result["rate"])
    return result


def run_multi(args, settings: Settings) -> list[dict]:
    if args.workers <= 1:
        return [generate(
            0, args.rate, args.duration, args.total, args.seed, args.drift,
            args.hardness, args.batch, settings,
            truth_path=args.truth, quiet=args.quiet,
        )]
    ctx = mp.get_context("spawn")
    per_worker_rate = args.rate / args.workers if args.rate else 0
    per_worker_total = args.total // args.workers if args.total else 0
    with ctx.Pool(args.workers) as pool:
        jobs = [
            pool.apply_async(generate, (
                i, per_worker_rate, args.duration, per_worker_total, args.seed,
                args.drift, args.hardness, args.batch, settings,
                f"{args.truth}.{i}" if args.truth else None, args.quiet,
            ))
            for i in range(args.workers)
        ]
        return [j.get() for j in jobs]


def merge_truth(base: str, workers: int) -> None:
    """Fold per-worker truth files into one."""
    if workers <= 1:
        return
    ids: list[str] = []
    classes: list[np.ndarray] = []
    for i in range(workers):
        part = Path(f"{base}.{i}")
        if part.exists():
            part_ids, part_classes = read_truth(part)
            ids.extend(part_ids)
            classes.append(part_classes)
            part.unlink()
    if ids:
        write_truth(Path(base), ids, np.concatenate(classes))


def main(argv=None) -> None:
    args = parse_args(argv)
    settings = Settings()
    if args.bus:
        settings.bus = args.bus
    setup_logging(settings.log_level)
    if args.reset:
        bus = make_bus(settings.bus, settings.redis_url, settings.stream_maxlen)
        if hasattr(bus, "delete"):
            bus.delete(settings.conn_stream, settings.feature_stream,
                       settings.alert_stream, settings.score_stream)
            log.info("reset streams")
        bus.close()
    t0 = time.perf_counter()
    results = run_multi(args, settings)
    if args.truth:
        merge_truth(args.truth, args.workers)
    elapsed = time.perf_counter() - t0
    total = sum(r["emitted"] for r in results)
    print(f"generated {total:,} connection records in {elapsed:.2f}s "
          f"= {total / elapsed:,.0f} conn/s across {args.workers} worker(s)")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="CyberNexus synthetic traffic generator")
    p.add_argument("--rate", type=float, default=10_000, help="target conn/s (0 = unlimited)")
    p.add_argument("--duration", type=float, default=30.0, help="seconds (0 = until --total)")
    p.add_argument("--total", type=int, default=0, help="stop after N records")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--batch", type=int, default=512)
    p.add_argument("--seed", type=int, default=99991, help="differs from the training seed")
    p.add_argument("--drift", type=float, default=0.0,
                   help="distribution shift vs training (0 = same environment)")
    p.add_argument("--hardness", type=float, default=DEFAULT_HARDNESS,
                   help="fraction of flows blended toward the opposite label")
    p.add_argument("--bus", default=None, choices=["redis", "memory"])
    p.add_argument("--truth", default=None, help="write flow_id -> class map here")
    p.add_argument("--reset", action="store_true", help="delete existing streams first")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args(argv)


if __name__ == "__main__":
    main()
