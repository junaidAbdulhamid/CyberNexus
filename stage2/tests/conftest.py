"""Shared fixtures for the Stage 2 backend tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def example_topology():
    from cnmap.models import Topology
    from discovery.synthesize import synthesize

    path = ROOT / "data/example-topology.json"
    if path.exists():
        return Topology.model_validate_json(path.read_text())
    return synthesize(120, seed=7)


@pytest.fixture
def store(example_topology):
    from cnmap.store import MapStore

    return MapStore(example_topology.model_copy(deep=True), half_life_s=60.0)


@pytest.fixture
def client(monkeypatch, tmp_path, example_topology):
    """TestClient against the app with Redis and the connector disabled."""
    monkeypatch.setenv("CN_MAP_USE_REDIS", "0")
    monkeypatch.setenv("CN_MAP_CONNECTOR", "0")
    monkeypatch.setenv("CN_MAP_API_KEY", "test-key")
    path = tmp_path / "topology.json"
    path.write_text(example_topology.model_dump_json())
    monkeypatch.setenv("CN_MAP_TOPOLOGY", str(path))

    import importlib

    import cnmap.config as config_module
    importlib.reload(config_module)
    import backend.auth as auth_module
    importlib.reload(auth_module)
    import backend.app as app_module
    importlib.reload(app_module)

    from fastapi.testclient import TestClient

    with TestClient(app_module.app) as test_client:
        test_client.headers.update({"X-API-Key": "test-key"})
        yield test_client


@pytest.fixture
def auth_headers():
    return {"X-API-Key": "test-key"}
