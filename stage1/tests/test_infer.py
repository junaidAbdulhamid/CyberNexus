import json

import numpy as np
import pytest

from cybernexus.features import FEATURE_NAMES, N_FEATURES
from model.infer import InferenceEngine, MicroBatcher, ModelNotFound, load_meta


def test_missing_model_raises(tmp_path):
    with pytest.raises(ModelNotFound):
        load_meta(tmp_path)


def test_metadata_matches_feature_set(model_dir):
    meta = load_meta(model_dir)
    assert meta.feature_names == list(FEATURE_NAMES)
    assert 0.0 < meta.threshold < 1.0


def test_scores_are_probabilities(model_dir):
    engine = InferenceEngine(model_dir=model_dir)
    X = np.random.default_rng(0).standard_normal((256, N_FEATURES)).astype(np.float32)
    scores = engine.score(X)
    assert scores.shape == (256,)
    assert ((scores >= 0) & (scores <= 1)).all()


def test_empty_input(model_dir):
    assert InferenceEngine(model_dir=model_dir).score(np.empty((0, N_FEATURES))).shape == (0,)


def test_predict_applies_threshold(model_dir):
    engine = InferenceEngine(model_dir=model_dir, threshold=0.5)
    X = np.random.default_rng(1).standard_normal((100, N_FEATURES)).astype(np.float32)
    scores, flags = engine.predict(X)
    assert np.array_equal(flags, scores >= 0.5)


def test_onnx_and_joblib_agree(model_dir):
    """The exported graph must match the pickled pipeline, or the served model
    is not the model that was evaluated."""
    if not (model_dir / "model.onnx").exists():
        pytest.skip("no ONNX export")
    X = np.random.default_rng(2).standard_normal((512, N_FEATURES)).astype(np.float32)
    onnx = InferenceEngine(model_dir=model_dir, backend="onnx").score(X)
    joblib = InferenceEngine(model_dir=model_dir, backend="joblib").score(X)
    assert np.allclose(onnx, joblib, atol=1e-4)


def test_feature_mismatch_is_rejected(tmp_path, model_dir):
    import shutil

    for name in ("model.joblib", "model.onnx", "metadata.json"):
        src = model_dir / name
        if src.exists():
            shutil.copy(src, tmp_path / name)
    meta = json.loads((tmp_path / "metadata.json").read_text())
    meta["feature_names"] = meta["feature_names"][:-1]
    (tmp_path / "metadata.json").write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="different feature set"):
        InferenceEngine(model_dir=tmp_path)


def test_detects_attacks_on_generated_traffic(model_dir):
    from cybernexus.features import extract_matrix
    from cybernexus.records import NUM_INDEX
    from cybernexus.trafficmodel import TrafficModel

    mat, _ = TrafficModel(seed=4242).sample_matrix(20_000)
    X, y = extract_matrix(mat), mat[:, NUM_INDEX["label"]].astype(int)
    engine = InferenceEngine(model_dir=model_dir)
    _, flags = engine.predict(X)
    recall = flags[y == 1].mean()
    fpr = flags[y == 0].mean()
    assert recall >= 0.90, f"recall {recall:.3f} on unseen simulated traffic"
    assert fpr <= 0.05, f"false positive rate {fpr:.3f}"


def test_microbatcher_matches_direct_scoring(model_dir):
    engine = InferenceEngine(model_dir=model_dir)
    batcher = MicroBatcher(engine, max_batch=8, max_delay=0.01)
    X = np.random.default_rng(3).standard_normal((5, N_FEATURES)).astype(np.float32)
    direct = engine.score(X)
    for i in range(5):
        assert batcher.submit(X[i:i + 1]) == pytest.approx(float(direct[i]), abs=1e-5)


def test_benchmark_reports_rows_per_sec(model_dir):
    result = InferenceEngine(model_dir=model_dir).benchmark(n=4096, batch=512, repeats=1)
    assert result["rows_per_sec"] > 1000
    assert result["batch_latency_ms_p50"] > 0
