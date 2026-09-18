"""Where the time goes.

Two views:

``--stages``   wall-clock cost of each step for one batch, repeated: generate,
               pack, publish, unpack, extract features, infer.  This is the
               number to look at when deciding what to optimise next.
``--profile``  cProfile over the ingest+score path, top functions by cumulative
               time.

    python -m bench.profile_pipeline --stages --batch 512 --repeats 200
    python -m bench.profile_pipeline --profile --batch 512 --repeats 200
"""
from __future__ import annotations

import argparse
import cProfile
import io
import pstats
import time

from cybernexus.bus import MemoryBus
from cybernexus.config import Settings
from cybernexus.features import extract_matrix, numeric_matrix
from cybernexus.records import NUM_INDEX, pack_batch, unpack_batch
from cybernexus.trafficmodel import TrafficModel
from cybernexus.wire import pack_features, unpack_features
from model.infer import InferenceEngine


def stage_timings(batch: int, repeats: int, backend: str | None) -> None:
    model = TrafficModel(seed=1)
    engine = InferenceEngine(backend=backend)
    settings = Settings()
    bus = MemoryBus(maxlen=repeats + 16)
    timings: dict[str, float] = {}

    def timed(name: str, fn, *a, **k):
        t0 = time.perf_counter()
        out = fn(*a, **k)
        timings[name] = timings.get(name, 0.0) + (time.perf_counter() - t0)
        return out

    for _ in range(repeats):
        recs, _ = timed("1_generate", model.sample_records, batch)
        blob = timed("2_pack_records", pack_batch, recs)
        timed("3_publish", bus.publish, settings.conn_stream, [blob])
        got = timed("4_unpack_records", unpack_batch, blob)
        mat = timed("5_numeric_matrix", numeric_matrix, got)
        X = timed("6_extract_features", extract_matrix, mat)
        fblob = timed("7_pack_features", pack_features,
                      [r[0] for r in got], [(r[1], r[2], r[3], r[4], r[5]) for r in got], X,
                      mat[:, NUM_INDEX["label"]], mat[:, NUM_INDEX["emit_ts"]])
        d = timed("8_unpack_features", unpack_features, fblob)
        timed("9_inference", engine.score, d["x"])

    total_records = batch * repeats
    total_time = sum(timings.values())
    print(f"\n{total_records:,} records, batch={batch}, backend={engine.backend}\n")
    print(f"{'stage':<22}{'total s':>10}{'us/record':>12}{'share':>9}{'rec/s':>14}")
    print("-" * 67)
    for name in sorted(timings):
        t = timings[name]
        print(f"{name:<22}{t:>10.3f}{t / total_records * 1e6:>12.2f}"
              f"{t / total_time:>8.1%}{total_records / t:>14,.0f}")
    print("-" * 67)
    print(f"{'TOTAL':<22}{total_time:>10.3f}{total_time / total_records * 1e6:>12.2f}"
          f"{1.0:>8.1%}{total_records / total_time:>14,.0f}")
    print("\nNote: a real deployment runs these stages in parallel processes, so "
          "single-process\nthroughput is bounded by the slowest stage, not by this sum.")
    slowest = max(timings, key=timings.get)
    print(f"Slowest stage: {slowest} "
          f"({total_records / timings[slowest]:,.0f} rec/s on one core)")


def profile_path(batch: int, repeats: int, backend: str | None, top: int) -> None:
    model = TrafficModel(seed=1)
    engine = InferenceEngine(backend=backend)
    blobs = [pack_batch(model.sample_records(batch)[0]) for _ in range(repeats)]

    def work():
        for blob in blobs:
            recs = unpack_batch(blob)
            mat = numeric_matrix(recs)
            X = extract_matrix(mat)
            engine.score(X)

    pr = cProfile.Profile()
    pr.enable()
    work()
    pr.disable()
    buf = io.StringIO()
    pstats.Stats(pr, stream=buf).sort_stats("cumulative").print_stats(top)
    print(buf.getvalue())


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Profile the CyberNexus hot path")
    p.add_argument("--stages", action="store_true", help="per-stage wall clock (default)")
    p.add_argument("--profile", action="store_true", help="cProfile the ingest+score path")
    p.add_argument("--batch", type=int, default=512)
    p.add_argument("--repeats", type=int, default=200)
    p.add_argument("--backend", default=None, choices=["auto", "onnx", "joblib"])
    p.add_argument("--top", type=int, default=25)
    args = p.parse_args(argv)
    if args.profile:
        profile_path(args.batch, args.repeats, args.backend, args.top)
    else:
        stage_timings(args.batch, args.repeats, args.backend)


if __name__ == "__main__":
    main()
