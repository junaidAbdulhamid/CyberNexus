"""Deterministic end-to-end benchmark.

Starts the full pipeline — generator -> conn stream -> ingest -> feature stream
-> scorer -> alert/score streams — drives it at a target connection rate, waits
for it to drain, then reports detection quality and throughput against the
ground truth carried by the generated records.

What "sustained" means here: the harness samples the connection-stream backlog
throughout the run.  A rate is only reported as sustained if the backlog stays
bounded — a pipeline that accepts 10k/s while falling permanently behind is not
running at 10k/s, it is buffering.

Everything is seeded, so two runs on the same box agree to within scheduling
noise:

    python -m bench.run_bench --rate 10000 --duration 30
    python -m bench.run_bench --rate 0 --duration 20      # find the ceiling
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from bench.evaluate import detection_metrics, format_report, latency_stats, per_class
from bench.generator import read_truth
from cybernexus.bus import make_bus
from cybernexus.config import ARTIFACT_DIR, Settings
from cybernexus.logging_setup import setup_logging
from cybernexus.trafficmodel import CLASS_NAMES
from cybernexus.wire import unpack_scores
from model.infer import load_meta

log = logging.getLogger("bench")


class ScoreCollector:
    """Drains the score stream in a background thread while the run proceeds."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.bus = make_bus(settings.bus, settings.redis_url, settings.stream_maxlen)
        self.ids: list[str] = []
        self.scores: list[np.ndarray] = []
        self.labels: list[np.ndarray] = []
        self.latency: list[np.ndarray] = []
        self.first_seen = 0.0
        self.last_seen = 0.0
        self.running = True
        self._thread = threading.Thread(target=self._loop, name="score-collector", daemon=True)

    def start(self) -> None:
        self.bus.ensure_group(self.settings.score_stream, "bench")
        self._thread.start()

    def _loop(self) -> None:
        while self.running:
            batch = self.bus.consume(self.settings.score_stream, "bench", "bench-0", 64, 250)
            if not batch:
                continue
            now = time.perf_counter()
            if not self.first_seen:
                self.first_seen = now
            self.last_seen = now
            for mid, blob in batch:
                d = unpack_scores(blob)
                self.ids.extend(d["ids"])
                self.scores.append(d["s"])
                if d["y"] is not None:
                    self.labels.append(d["y"])
                if d["l"] is not None:
                    self.latency.append(d["l"])
            self.bus.ack(self.settings.score_stream, "bench", [mid for mid, _ in batch])

    def stop(self) -> None:
        self.running = False
        self._thread.join(timeout=5)

    @property
    def count(self) -> int:
        return len(self.ids)

    def arrays(self):
        cat = lambda parts: np.concatenate(parts) if parts else np.empty(0, dtype=np.float32)
        return self.ids, cat(self.scores), cat(self.labels), cat(self.latency)


class BacklogSampler:
    """Polls stream depth so the report can say whether the pipeline kept up."""

    def __init__(self, settings: Settings, interval: float = 0.25):
        self.settings = settings
        self.interval = interval
        self.bus = make_bus(settings.bus, settings.redis_url, settings.stream_maxlen)
        self.samples: list[tuple[float, int, int]] = []
        self.running = True
        self._thread = threading.Thread(target=self._loop, name="backlog", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _loop(self) -> None:
        # Stream length is the number of *retained* entries, not the backlog, so
        # the unacked depth is taken from the consumer group where available.
        while self.running:
            t = time.perf_counter()
            self.samples.append((t, self._pending(self.settings.conn_stream, self.settings.ingest_group),
                                 self._pending(self.settings.feature_stream, self.settings.scorer_group)))
            time.sleep(self.interval)

    def _pending(self, stream: str, group: str) -> int:
        try:
            client = getattr(self.bus, "client", None)
            if client is None:
                return self.bus.length(stream)
            info = client.xinfo_groups(stream)
            for g in info:
                name = g.get("name")
                name = name.decode() if isinstance(name, bytes) else name
                if name == group:
                    return int(g.get("lag") or g.get("pending") or 0)
            return int(client.xlen(stream))
        except Exception:
            return 0

    def stop(self) -> None:
        self.running = False
        self._thread.join(timeout=3)

    def summary(self) -> dict:
        if not self.samples:
            return {"max_conn_backlog": 0, "max_feature_backlog": 0, "bounded": True}
        conn = [c for _, c, _ in self.samples]
        feat = [f for _, _, f in self.samples]
        # "Growing" = the last third of the run is deeper than the first third
        # by more than a batch, i.e. the queue never recovers.
        third = max(len(conn) // 3, 1)
        drifting = (np.mean(conn[-third:]) > np.mean(conn[:third]) + 5000)
        return {
            "max_conn_backlog": int(max(conn)),
            "max_feature_backlog": int(max(feat)),
            "mean_conn_backlog": float(np.mean(conn)),
            "final_conn_backlog": int(conn[-1]),
            "bounded": bool(not drifting),
        }


def spawn(cmd: list[str], env: dict, log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "wb")
    return subprocess.Popen(cmd, env=env, stdout=handle, stderr=subprocess.STDOUT)


def run(args) -> dict:
    settings = Settings()
    settings.bus = args.bus
    settings.emit_scores = True
    setup_logging(args.log_level)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    truth_path = out_dir / "truth.msgpack"

    if settings.bus != "redis":
        raise SystemExit(
            "the end-to-end bench needs the Redis bus (the in-memory bus is "
            "per-process). Start Redis, or run the single-process path:  "
            "python -m bench.inproc_bench"
        )

    meta = load_meta(Path(args.model_dir or settings.model_dir))
    threshold = args.threshold if args.threshold is not None else meta.threshold

    bus = make_bus(settings.bus, settings.redis_url, settings.stream_maxlen)
    if not getattr(bus, "ping", lambda: True)():
        raise SystemExit(f"cannot reach Redis at {settings.redis_url}")
    bus.delete(settings.conn_stream, settings.feature_stream,
               settings.alert_stream, settings.score_stream)
    log.info("streams reset")

    env = dict(os.environ)
    env.update({
        "CN_BUS": settings.bus,
        "CN_REDIS_URL": settings.redis_url,
        "CN_EMIT_SCORES": "1",
        "CN_PUBLISH_BATCH": str(args.batch),
        "CN_READ_BATCH": str(args.read_batch),
        "CN_LOG_LEVEL": args.worker_log_level,
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
    })
    if args.model_dir:
        env["CN_MODEL_DIR"] = args.model_dir
    if args.threshold is not None:
        env["CN_THRESHOLD"] = str(args.threshold)
    if args.affinity:
        env["CN_CPU_AFFINITY"] = args.affinity

    procs: list[subprocess.Popen] = []
    idle_exit = str(args.drain_timeout)
    for i in range(args.ingest_workers):
        procs.append(spawn(
            [sys.executable, "-m", "ingest.consumer", "--workers", "1",
             "--idle-exit", idle_exit] + (["--aggregate"] if args.aggregate else []),
            env, out_dir / f"ingest-{i}.log"))
    for i in range(args.scorer_workers):
        cmd = [sys.executable, "-m", "model.scorer", "--workers", "1",
               "--idle-exit", idle_exit, "--emit-scores"]
        if args.backend:
            cmd += ["--backend", args.backend]
        procs.append(spawn(cmd, env, out_dir / f"scorer-{i}.log"))
    log.info("started %d ingest + %d scorer worker(s); warming up",
             args.ingest_workers, args.scorer_workers)
    time.sleep(args.warmup)

    collector = ScoreCollector(settings)
    collector.start()
    sampler = BacklogSampler(settings)
    sampler.start()

    gen_cmd = [
        sys.executable, "-m", "bench.generator",
        "--rate", str(args.rate), "--duration", str(args.duration),
        "--workers", str(args.gen_workers), "--batch", str(args.batch),
        "--seed", str(args.seed), "--drift", str(args.drift),
        "--hardness", str(args.hardness), "--truth", str(truth_path), "--quiet",
    ]
    if args.total:
        gen_cmd += ["--total", str(args.total)]
    log.info("generating: %s", " ".join(gen_cmd[2:]))
    t_gen0 = time.perf_counter()
    gen = subprocess.run(gen_cmd, env=env, capture_output=True, text=True)
    t_gen1 = time.perf_counter()
    if gen.returncode != 0:
        log.error("generator failed: %s", gen.stderr[-2000:])
        raise SystemExit(1)
    gen_line = gen.stdout.strip().splitlines()[-1] if gen.stdout.strip() else ""
    log.info("generator: %s", gen_line)

    # --- drain -----------------------------------------------------------
    log.info("draining (up to %.0fs)", args.drain_timeout)
    drain_deadline = time.perf_counter() + args.drain_timeout
    stable_for = 0.0
    last_count = -1
    while time.perf_counter() < drain_deadline:
        time.sleep(0.5)
        if collector.count == last_count:
            stable_for += 0.5
            if stable_for >= 2.0:
                break
        else:
            stable_for = 0.0
            last_count = collector.count
    collector.stop()
    sampler.stop()
    for p in procs:
        p.terminate()
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            p.kill()

    # --- evaluate --------------------------------------------------------
    ids, scores, labels, latency = collector.arrays()
    if not len(scores):
        raise SystemExit("no scores were produced - check the worker logs in " + str(out_dir))
    n_gen = int(np.round(float(gen_line.split()[1].replace(",", "")))) if gen_line else len(ids)

    detection = detection_metrics(labels, scores, threshold)
    lat = latency_stats(latency)
    scoring_window = max(collector.last_seen - collector.first_seen, 1e-9)
    gen_window = max(t_gen1 - t_gen0, 1e-9)
    sustained = len(scores) / scoring_window
    backlog = sampler.summary()

    classes = {}
    if truth_path.exists():
        truth_ids, truth_classes = read_truth(truth_path)
        index = {fid: i for i, fid in enumerate(truth_ids)}
        mapped = np.array([index.get(fid, -1) for fid in ids])
        found = mapped >= 0
        classes = per_class(
            truth_classes[mapped[found]], (scores[found] >= threshold), CLASS_NAMES
        )

    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": {
            "bus": settings.bus,
            "target_rate": args.rate,
            "duration_s": args.duration,
            "batch": args.batch,
            "read_batch": args.read_batch,
            "gen_workers": args.gen_workers,
            "ingest_workers": args.ingest_workers,
            "scorer_workers": args.scorer_workers,
            "seed": args.seed,
            "drift": args.drift,
            "hardness": args.hardness,
            "aggregate": args.aggregate,
        },
        "model": {
            "name": meta.model,
            "backend": args.backend or "auto",
            "trained_utc": meta.created_utc,
            "model_dir": str(args.model_dir or settings.model_dir),
        },
        "throughput": {
            "target_rate": args.rate,
            "offered_rate": n_gen / gen_window,
            "generated": n_gen,
            "scored": int(len(scores)),
            "scoring_window_s": scoring_window,
            "sustained_rate": sustained,
            "max_backlog": backlog["max_conn_backlog"],
            "backlog_bounded": backlog["bounded"],
            "backlog": backlog,
            "loss_rate": max(0.0, 1.0 - len(scores) / max(n_gen, 1)),
        },
        "detection": detection,
        "latency": lat,
        "per_class": classes,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
        },
    }
    report["acceptance"] = {
        "detection_target": args.detection_target,
        "detection_pass": bool(detection["detection_rate"] >= args.detection_target),
        "throughput_target": args.throughput_target,
        "throughput_pass": bool(
            sustained >= args.throughput_target and backlog["bounded"]
        ),
    }

    (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=float))
    text = format_report(report)
    (out_dir / "report.txt").write_text(text)
    print(text)
    log.info("report written to %s", out_dir / "report.json")
    bus.close()
    return report


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="CyberNexus end-to-end benchmark")
    p.add_argument("--rate", type=float, default=10_000, help="offered conn/s (0 = unlimited)")
    p.add_argument("--duration", type=float, default=30.0)
    p.add_argument("--total", type=int, default=0)
    p.add_argument("--gen-workers", type=int, default=2)
    p.add_argument("--ingest-workers", type=int, default=2)
    p.add_argument("--scorer-workers", type=int, default=2)
    p.add_argument("--batch", type=int, default=512)
    p.add_argument("--read-batch", type=int, default=16)
    p.add_argument("--backend", default=None, choices=["auto", "onnx", "joblib"])
    p.add_argument("--model-dir", default=None)
    p.add_argument("--threshold", type=float, default=None)
    p.add_argument("--bus", default="redis", choices=["redis"])
    p.add_argument("--seed", type=int, default=99991)
    p.add_argument("--drift", type=float, default=0.0)
    p.add_argument("--hardness", type=float, default=None)
    p.add_argument("--aggregate", action="store_true")
    p.add_argument("--affinity", default="", help='CPU list for workers, e.g. "2,3,4,5"')
    p.add_argument("--warmup", type=float, default=2.0)
    p.add_argument("--drain-timeout", type=float, default=60.0)
    p.add_argument("--detection-target", type=float, default=0.96)
    p.add_argument("--throughput-target", type=float, default=10_000.0)
    p.add_argument("--out", default=str(ARTIFACT_DIR / "bench"))
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--worker-log-level", default="INFO")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    if args.hardness is None:
        from cybernexus.trafficmodel import DEFAULT_HARDNESS

        args.hardness = DEFAULT_HARDNESS
    report = run(args)
    ok = report["acceptance"]["detection_pass"] and report["acceptance"]["throughput_pass"]
    sys.exit(0 if ok else 2)
