"""Environment-driven configuration for every CyberNexus service.

All services read the same object so that a single set of environment variables
(or a `.env` exported into the shell) configures capture, ingest, scoring, the
API and the benchmark harness identically.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_DIR = Path(os.getenv("CN_ARTIFACT_DIR", REPO_ROOT / "artifacts"))


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # --- message bus -----------------------------------------------------
    bus: str = field(default_factory=lambda: os.getenv("CN_BUS", "redis"))
    redis_url: str = field(
        default_factory=lambda: os.getenv("CN_REDIS_URL", "redis://localhost:6379/0")
    )
    conn_stream: str = field(default_factory=lambda: os.getenv("CN_CONN_STREAM", "cn:conns"))
    feature_stream: str = field(default_factory=lambda: os.getenv("CN_FEATURE_STREAM", "cn:features"))
    alert_stream: str = field(default_factory=lambda: os.getenv("CN_ALERT_STREAM", "cn:alerts"))
    score_stream: str = field(default_factory=lambda: os.getenv("CN_SCORE_STREAM", "cn:scores"))
    ingest_group: str = field(default_factory=lambda: os.getenv("CN_INGEST_GROUP", "ingest"))
    scorer_group: str = field(default_factory=lambda: os.getenv("CN_SCORER_GROUP", "scorer"))
    # Approximate stream trimming keeps memory bounded under sustained load.
    stream_maxlen: int = field(default_factory=lambda: _env_int("CN_STREAM_MAXLEN", 2_000_000))

    # --- batching / concurrency -----------------------------------------
    publish_batch: int = field(default_factory=lambda: _env_int("CN_PUBLISH_BATCH", 512))
    read_batch: int = field(default_factory=lambda: _env_int("CN_READ_BATCH", 16))
    block_ms: int = field(default_factory=lambda: _env_int("CN_BLOCK_MS", 200))
    # Wait at most this long before flushing a partial batch (latency guard).
    flush_interval_s: float = field(default_factory=lambda: _env_float("CN_FLUSH_INTERVAL_S", 0.05))
    ingest_workers: int = field(default_factory=lambda: _env_int("CN_INGEST_WORKERS", 2))
    scorer_workers: int = field(default_factory=lambda: _env_int("CN_SCORER_WORKERS", 2))
    cpu_affinity: str = field(default_factory=lambda: os.getenv("CN_CPU_AFFINITY", ""))

    # --- model / inference ----------------------------------------------
    model_dir: Path = field(default_factory=lambda: Path(os.getenv("CN_MODEL_DIR", ARTIFACT_DIR / "model")))
    backend: str = field(default_factory=lambda: os.getenv("CN_BACKEND", "auto"))  # auto|onnx|joblib
    threshold: float | None = field(
        default_factory=lambda: float(os.environ["CN_THRESHOLD"]) if os.getenv("CN_THRESHOLD") else None
    )
    ort_threads: int = field(default_factory=lambda: _env_int("CN_ORT_THREADS", 1))
    emit_scores: bool = field(default_factory=lambda: _env_bool("CN_EMIT_SCORES", False))

    # --- capture ---------------------------------------------------------
    iface: str = field(default_factory=lambda: os.getenv("CN_IFACE", "lo0"))
    bpf_filter: str = field(default_factory=lambda: os.getenv("CN_BPF", "ip or ip6"))
    flow_idle_timeout_s: float = field(default_factory=lambda: _env_float("CN_FLOW_IDLE_TIMEOUT", 15.0))
    flow_active_timeout_s: float = field(default_factory=lambda: _env_float("CN_FLOW_ACTIVE_TIMEOUT", 120.0))
    max_flows: int = field(default_factory=lambda: _env_int("CN_MAX_FLOWS", 250_000))
    # Privacy: raw payload capture is opt-in and off by default.
    store_payloads: bool = field(default_factory=lambda: _env_bool("CN_STORE_PAYLOADS", False))
    payload_bytes: int = field(default_factory=lambda: _env_int("CN_PAYLOAD_BYTES", 64))
    anonymize_ips: bool = field(default_factory=lambda: _env_bool("CN_ANONYMIZE_IPS", False))
    anonymize_salt: str = field(default_factory=lambda: os.getenv("CN_ANONYMIZE_SALT", "cybernexus"))

    # --- api -------------------------------------------------------------
    api_host: str = field(default_factory=lambda: os.getenv("CN_API_HOST", "0.0.0.0"))
    api_port: int = field(default_factory=lambda: _env_int("CN_API_PORT", 8000))
    alert_buffer: int = field(default_factory=lambda: _env_int("CN_ALERT_BUFFER", 5000))

    log_level: str = field(default_factory=lambda: os.getenv("CN_LOG_LEVEL", "INFO"))

    def model_path(self, name: str) -> Path:
        return self.model_dir / name


settings = Settings()
