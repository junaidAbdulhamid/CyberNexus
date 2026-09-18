"""Low-latency inference wrapper.

Loads whatever ``model/train.py`` exported and exposes one call — ``score`` on a
feature matrix.  Nothing here knows how the model was trained; swapping the
model means pointing ``CN_MODEL_DIR`` at a different artifact directory.

Backends
--------
``onnx``    ONNX Runtime, one session per worker process with
            ``intra_op_num_threads=1``.  Single-threaded sessions are the right
            choice here: the pipeline already scales by running N worker
            processes, and letting each session spawn its own thread pool
            oversubscribes the CPU and *lowers* aggregate throughput.
``joblib``  The pickled scikit-learn pipeline.  Used when ONNX Runtime is not
            installed or the export failed; identical numerics, slower for the
            tree model.

``auto`` (the default) prefers ONNX and falls back to joblib.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from cybernexus.config import settings
from cybernexus.features import FEATURE_NAMES

log = logging.getLogger(__name__)


class ModelNotFound(FileNotFoundError):
    pass


@dataclass
class ModelMeta:
    threshold: float
    feature_names: list[str]
    model: str
    created_utc: str
    raw: dict

    @property
    def n_features(self) -> int:
        return len(self.feature_names)


def load_meta(model_dir: Path) -> ModelMeta:
    path = Path(model_dir) / "metadata.json"
    if not path.exists():
        raise ModelNotFound(
            f"no model metadata at {path}. Train one first:  python -m model.train"
        )
    raw = json.loads(path.read_text())
    return ModelMeta(
        threshold=float(raw["threshold"]),
        feature_names=list(raw["feature_names"]),
        model=raw.get("model", "unknown"),
        created_utc=raw.get("created_utc", ""),
        raw=raw,
    )


class InferenceEngine:
    """Thread-safe (via a lock) batch scorer.  One instance per worker process."""

    def __init__(
        self,
        model_dir: str | Path | None = None,
        backend: str | None = None,
        threshold: float | None = None,
        ort_threads: int | None = None,
    ):
        self.model_dir = Path(model_dir or settings.model_dir)
        self.meta = load_meta(self.model_dir)
        self.threshold = (
            threshold if threshold is not None
            else (settings.threshold if settings.threshold is not None else self.meta.threshold)
        )
        if list(self.meta.feature_names) != list(FEATURE_NAMES):
            raise ValueError(
                "model was trained on a different feature set than the running "
                "extractor; retrain or pin the matching code revision"
            )
        self._lock = threading.Lock()
        self.backend = self._load(backend or settings.backend, ort_threads)
        log.info(
            "inference ready: backend=%s model=%s threshold=%.4f features=%d",
            self.backend, self.meta.model, self.threshold, self.meta.n_features,
        )

    # -- loading ---------------------------------------------------------
    def _load(self, backend: str, ort_threads: int | None) -> str:
        backend = (backend or "auto").lower()
        onnx_path = self.model_dir / "model.onnx"
        if backend in {"auto", "onnx"} and onnx_path.exists():
            try:
                import onnxruntime as ort

                opts = ort.SessionOptions()
                opts.intra_op_num_threads = ort_threads or settings.ort_threads
                opts.inter_op_num_threads = 1
                opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                self._sess = ort.InferenceSession(
                    str(onnx_path), sess_options=opts, providers=["CPUExecutionProvider"]
                )
                self._input = self._sess.get_inputs()[0].name
                # Output 0 is the label, output 1 the probabilities (zipmap off).
                outs = self._sess.get_outputs()
                self._prob_out = outs[1].name if len(outs) > 1 else outs[0].name
                return "onnx"
            except Exception as exc:
                if backend == "onnx":
                    raise
                log.warning("ONNX backend unavailable (%s); falling back to joblib", exc)
        if backend in {"auto", "joblib"}:
            import joblib

            path = self.model_dir / "model.joblib"
            if not path.exists():
                raise ModelNotFound(f"no model artifact in {self.model_dir}")
            self._pipeline = joblib.load(path)
            return "joblib"
        raise ValueError(f"unknown backend {backend!r}")

    # -- scoring ---------------------------------------------------------
    def score(self, X: np.ndarray) -> np.ndarray:
        """Attack probability for each row of ``X`` (n, n_features)."""
        if X.size == 0:
            return np.empty(0, dtype=np.float32)
        X = np.ascontiguousarray(X, dtype=np.float32)
        with self._lock:
            if self.backend == "onnx":
                out = self._sess.run([self._prob_out], {self._input: X})[0]
                probs = np.asarray(out)
                if probs.ndim == 2:
                    probs = probs[:, -1]
            else:
                probs = self._pipeline.predict_proba(X)[:, 1]
        return np.asarray(probs, dtype=np.float32).ravel()

    def predict(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        scores = self.score(X)
        return scores, scores >= self.threshold

    def benchmark(self, n: int = 65_536, batch: int = 1024, repeats: int = 3) -> dict:
        """Measure this backend's rows/sec and per-batch latency."""
        rng = np.random.default_rng(0)
        X = rng.standard_normal((n, self.meta.n_features)).astype(np.float32)
        self.score(X[:batch])  # warm up
        best, latencies = 0.0, []
        for _ in range(repeats):
            t0 = time.perf_counter()
            for start in range(0, n, batch):
                b0 = time.perf_counter()
                self.score(X[start:start + batch])
                latencies.append(time.perf_counter() - b0)
            best = max(best, n / (time.perf_counter() - t0))
        lat = np.array(latencies) * 1000.0
        return {
            "backend": self.backend,
            "rows_per_sec": best,
            "batch": batch,
            "batch_latency_ms_p50": float(np.percentile(lat, 50)),
            "batch_latency_ms_p99": float(np.percentile(lat, 99)),
            "per_row_us": float(np.mean(lat) / batch * 1000.0),
        }


class MicroBatcher:
    """Collects single-flow scoring requests into batches.

    Used by the REST ``/score`` endpoint, where requests arrive one at a time:
    waiting up to ``max_delay`` for up to ``max_batch`` rows turns N separate
    matrix multiplications into one, which is worth far more than the couple of
    milliseconds of added latency.  The streaming path never needs this — it is
    batched already by the bus.
    """

    def __init__(self, engine: InferenceEngine, max_batch: int = 256, max_delay: float = 0.005):
        self.engine = engine
        self.max_batch = max_batch
        self.max_delay = max_delay
        self._lock = threading.Lock()
        self._pending: list[tuple[np.ndarray, list]] = []
        self._timer: threading.Timer | None = None

    def submit(self, row: np.ndarray) -> float:
        """Blocking single-row score.  Returns the attack probability."""
        box: list = []
        event = threading.Event()
        with self._lock:
            self._pending.append((row, box))
            box.append(event)
            should_flush = len(self._pending) >= self.max_batch
            if should_flush:
                pending, self._pending = self._pending, []
            elif self._timer is None:
                self._timer = threading.Timer(self.max_delay, self._flush_timer)
                self._timer.daemon = True
                self._timer.start()
        if should_flush:
            self._run(pending)
        event.wait(timeout=5.0)
        return float(box[1]) if len(box) > 1 else 0.0

    def _flush_timer(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, []
            self._timer = None
        self._run(pending)

    def _run(self, pending: list) -> None:
        if not pending:
            return
        X = np.vstack([row for row, _ in pending])
        scores = self.engine.score(X)
        for (_, box), score in zip(pending, scores):
            box.append(float(score))
            box[0].set()


_engine: InferenceEngine | None = None


def get_engine(**kwargs) -> InferenceEngine:
    """Process-wide singleton; loading a session per batch would dominate cost."""
    global _engine
    if _engine is None:
        _engine = InferenceEngine(**kwargs)
    return _engine


if __name__ == "__main__":
    import argparse

    from cybernexus.logging_setup import setup_logging

    ap = argparse.ArgumentParser(description="Inference wrapper self-benchmark")
    ap.add_argument("--model-dir", default=None)
    ap.add_argument("--backend", default=None, choices=["auto", "onnx", "joblib"])
    ap.add_argument("--batch", type=int, default=1024)
    ap.add_argument("--n", type=int, default=131_072)
    args = ap.parse_args()
    setup_logging()
    engine = InferenceEngine(model_dir=args.model_dir, backend=args.backend)
    print(json.dumps(engine.benchmark(n=args.n, batch=args.batch), indent=2))
