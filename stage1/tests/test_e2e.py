"""End-to-end integration over the in-process bus.

Drives the real stage classes — the same ``FeatureStage`` and ``ScoreStage`` the
Redis deployment runs — from generated traffic through to alerts, with no
external services.  This is the test that fails if any two stages stop agreeing
about the wire format.
"""
import threading

import numpy as np

from cybernexus.bus import MemoryBus
from cybernexus.records import pack_batch
from cybernexus.service import StageRunner
from cybernexus.trafficmodel import TrafficModel
from cybernexus.wire import unpack_alerts, unpack_scores
from ingest.consumer import FeatureStage
from model.scorer import ScoreStage


def run_pipeline(settings, model_dir, n_records=20_000, seed=4242, aggregate=False):
    bus = MemoryBus(maxlen=200_000)
    ingest = FeatureStage(bus, settings, aggregate=aggregate)
    scorer = ScoreStage(bus, settings, model_dir=model_dir, emit_scores=True)
    runners = [
        StageRunner("ingest", bus, settings, settings.conn_stream, settings.ingest_group,
                    ingest.handle, idle_exit_s=1.5),
        StageRunner("scorer", bus, settings, settings.feature_stream, settings.scorer_group,
                    scorer.handle, idle_exit_s=2.5),
    ]
    threads = [threading.Thread(target=r.run, daemon=True) for r in runners]
    for t in threads:
        t.start()

    model = TrafficModel(seed=seed)
    produced = 0
    while produced < n_records:
        take = min(512, n_records - produced)
        recs, _ = model.sample_records(take)
        bus.publish(settings.conn_stream, [pack_batch(recs)])
        produced += take
    for t in threads:
        t.join(timeout=60)
    return bus, scorer, runners


def collect(bus, stream, unpack):
    out = []
    bus.ensure_group(stream, "test")
    while True:
        batch = bus.consume(stream, "test", "c", 256, 50)
        if not batch:
            return out
        for _mid, blob in batch:
            out.append(unpack(blob))


def test_pipeline_scores_every_connection(settings_for, model_dir):
    bus, scorer, runners = run_pipeline(settings_for, model_dir, n_records=20_000)
    scores = collect(bus, settings_for.score_stream, unpack_scores)
    n_scored = sum(len(d["ids"]) for d in scores)
    assert n_scored == 20_000, "every generated connection must reach a verdict"
    assert all(r.meter.total == 20_000 for r in runners)


def test_detection_quality_end_to_end(settings_for, model_dir):
    bus, scorer, _ = run_pipeline(settings_for, model_dir, n_records=30_000, seed=777)
    batches = collect(bus, settings_for.score_stream, unpack_scores)
    scores = np.concatenate([b["s"] for b in batches])
    labels = np.concatenate([b["y"] for b in batches])
    flagged = scores >= scorer.engine.threshold

    detection = flagged[labels == 1].mean()
    fpr = flagged[labels == 0].mean()
    assert detection >= 0.93, f"detection rate {detection:.4f}"
    assert fpr <= 0.05, f"false positive rate {fpr:.4f}"


def test_alerts_carry_usable_metadata(settings_for, model_dir):
    bus, scorer, _ = run_pipeline(settings_for, model_dir, n_records=10_000)
    alerts = [a for blob in collect(bus, settings_for.alert_stream, unpack_alerts) for a in blob]
    assert alerts, "an attack-bearing stream must raise alerts"
    assert len(alerts) == scorer.alerts
    for alert in alerts[:50]:
        assert alert["score"] >= alert["threshold"]
        assert alert["src_ip"] and alert["dst_ip"]
        assert 0 <= alert["dst_port"] <= 65535
        assert alert["protocol"] in (1, 6, 17)
        assert alert["latency_s"] >= 0
        assert alert["flow_id"]


def test_latency_is_measured(settings_for, model_dir):
    bus, _, _ = run_pipeline(settings_for, model_dir, n_records=5_000)
    batches = collect(bus, settings_for.score_stream, unpack_scores)
    latency = np.concatenate([b["l"] for b in batches])
    assert len(latency) == 5_000
    assert np.isfinite(latency).all() and (latency >= 0).all()
    assert np.percentile(latency, 50) < 5.0     # seconds; generous for CI


def test_aggregate_mode_merges_split_records(settings_for, model_dir):
    """Two halves of one flow must arrive at the model as a single connection."""
    bus = MemoryBus(maxlen=10_000)
    ingest = FeatureStage(bus, settings_for, aggregate=True)
    scorer = ScoreStage(bus, settings_for, model_dir=model_dir, emit_scores=True)
    ingest.scorer = scorer

    model = TrafficModel(seed=5)
    recs, _ = model.sample_records(100)
    halves = [tuple(r) for r in recs] + [tuple(r) for r in recs]  # each flow twice
    bus.publish(settings_for.conn_stream, [pack_batch(halves)])
    payloads = [p for _, p in bus.consume(settings_for.conn_stream, "g", "c", 10, 50)]
    n = ingest.handle(payloads)
    assert n == 100, "200 partial records must collapse into 100 connections"


def test_stage_handles_empty_batch(settings_for, model_dir):
    bus = MemoryBus()
    assert FeatureStage(bus, settings_for).handle([pack_batch([])]) == 0
