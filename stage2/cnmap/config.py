"""Environment-driven configuration for the Stage 2 services."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # --- data -----------------------------------------------------------
    topology_path: Path = field(
        default_factory=lambda: Path(os.getenv("CN_MAP_TOPOLOGY", REPO_ROOT / "data/example-topology.json"))
    )
    redis_url: str = field(default_factory=lambda: os.getenv("CN_MAP_REDIS_URL", os.getenv("CN_REDIS_URL", "redis://localhost:6379/0")))
    use_redis: bool = field(default_factory=lambda: _env_bool("CN_MAP_USE_REDIS", True))
    namespace: str = field(default_factory=lambda: os.getenv("CN_MAP_NAMESPACE", "cnmap"))

    # --- Stage 1 integration --------------------------------------------
    alert_stream: str = field(default_factory=lambda: os.getenv("CN_ALERT_STREAM", "cn:alerts"))
    alert_group: str = field(default_factory=lambda: os.getenv("CN_MAP_ALERT_GROUP", "cnmap"))
    connector_enabled: bool = field(default_factory=lambda: _env_bool("CN_MAP_CONNECTOR", True))
    #: "$" = live alerts only; "0" = replay everything in the stream.
    alert_start_id: str = field(default_factory=lambda: os.getenv("CN_MAP_ALERT_START", "$"))

    # --- behaviour ------------------------------------------------------
    half_life_s: float = field(default_factory=lambda: float(os.getenv("CN_MAP_HALF_LIFE", "120")))
    dwell_s: float = field(default_factory=lambda: float(os.getenv("CN_MAP_DWELL", "30")))
    decay_interval_s: float = field(default_factory=lambda: float(os.getenv("CN_MAP_DECAY_INTERVAL", "2.0")))
    timeline_size: int = field(default_factory=lambda: int(os.getenv("CN_MAP_TIMELINE_SIZE", "20000")))
    correlate_prefer: str = field(default_factory=lambda: os.getenv("CN_MAP_CORRELATE_PREFER", "internal"))

    # --- api ------------------------------------------------------------
    host: str = field(default_factory=lambda: os.getenv("CN_MAP_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.getenv("CN_MAP_PORT", "8080")))
    cors_origins: list[str] = field(
        default_factory=lambda: [o for o in os.getenv(
            "CN_MAP_CORS", "http://localhost:5173,http://127.0.0.1:5173"
        ).split(",") if o]
    )
    static_dir: Path = field(
        default_factory=lambda: Path(os.getenv("CN_MAP_STATIC", REPO_ROOT / "frontend/dist"))
    )

    # --- auth (stub) ----------------------------------------------------
    auth_enabled: bool = field(default_factory=lambda: _env_bool("CN_MAP_AUTH", True))
    api_key: str = field(default_factory=lambda: os.getenv("CN_MAP_API_KEY", "demo-key"))
    jwt_secret: str = field(default_factory=lambda: os.getenv("CN_MAP_JWT_SECRET", "demo-secret-change-me-in-production-0123456789"))
    jwt_ttl_s: int = field(default_factory=lambda: int(os.getenv("CN_MAP_JWT_TTL", "3600")))

    # --- ticketing ------------------------------------------------------
    webhook_url: str = field(default_factory=lambda: os.getenv("CN_MAP_WEBHOOK_URL", ""))
    webhook_token: str = field(default_factory=lambda: os.getenv("CN_MAP_WEBHOOK_TOKEN", ""))
    webhook_auto_severity: str = field(
        default_factory=lambda: os.getenv("CN_MAP_WEBHOOK_AUTO", "")
    )

    # --- demo -----------------------------------------------------------
    allow_simulation: bool = field(default_factory=lambda: _env_bool("CN_MAP_ALLOW_SIMULATION", True))
    log_level: str = field(default_factory=lambda: os.getenv("CN_MAP_LOG_LEVEL", "INFO"))


settings = Settings()
