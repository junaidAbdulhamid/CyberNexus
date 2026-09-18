"""Shared fixtures.

The model-dependent tests need a model.  Rather than skipping when none has
been trained, the fixture trains a small one into a temporary directory once per
session (a few seconds), so ``pytest`` passes on a clean checkout with no
prerequisite steps.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def model_dir(tmp_path_factory) -> Path:
    """A trained model directory: the real one if present, else a small one."""
    from cybernexus.config import settings

    existing = Path(settings.model_dir)
    if (existing / "metadata.json").exists():
        return existing
    out = tmp_path_factory.mktemp("model")
    from model.train import parse_args, train

    train(parse_args([
        "--n", "60000", "--model", "mlp", "--out", str(out), "--log-level", "WARNING",
    ]))
    return out


@pytest.fixture
def settings_for(tmp_path, model_dir):
    """Settings wired to the in-memory bus and unique stream names."""
    from cybernexus.config import Settings

    s = Settings()
    s.bus = "memory"
    s.model_dir = model_dir
    unique = os.urandom(4).hex()
    s.conn_stream = f"t:conns:{unique}"
    s.feature_stream = f"t:features:{unique}"
    s.alert_stream = f"t:alerts:{unique}"
    s.score_stream = f"t:scores:{unique}"
    s.block_ms = 50
    s.emit_scores = True
    return s


def pytest_sessionfinish(session, exitstatus):
    """Drop inference sessions before the interpreter tears down.

    ONNX Runtime occasionally aborts on macOS if its session objects are
    finalised during interpreter shutdown rather than before it; collecting them
    here keeps the test process exit code meaningful.
    """
    import gc

    try:
        import model.infer as infer

        infer._engine = None
    except Exception:
        pass
    gc.collect()
