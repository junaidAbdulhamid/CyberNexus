import json

import numpy as np
import pytest

from model.train import binary_metrics, choose_threshold, operating_points, parse_args, train


def test_binary_metrics_on_a_known_confusion_matrix():
    y = np.array([1, 1, 1, 0, 0, 0, 0])
    scores = np.array([0.9, 0.8, 0.2, 0.7, 0.1, 0.1, 0.1])
    m = binary_metrics(y, scores, 0.5)
    assert (m["tp"], m["fp"], m["fn"], m["tn"]) == (2, 1, 1, 3)
    assert m["precision"] == pytest.approx(2 / 3)
    assert m["recall"] == pytest.approx(2 / 3)
    assert m["detection_rate"] == m["recall"]
    assert m["fpr"] == pytest.approx(0.25)


def test_choose_threshold_meets_recall_target():
    rng = np.random.default_rng(0)
    y = np.concatenate([np.ones(2000), np.zeros(8000)]).astype(int)
    scores = np.concatenate([rng.normal(0.75, 0.15, 2000), rng.normal(0.25, 0.15, 8000)])
    scores = np.clip(scores, 0, 1)
    thr, rule = choose_threshold(y, scores, target_recall=0.9, max_fpr=0.2)
    recall = (scores[y == 1] >= thr).mean()
    assert recall >= 0.9
    assert "recall" in rule


def test_choose_threshold_falls_back_when_target_unreachable():
    y = np.array([1, 0, 1, 0])
    scores = np.array([0.5, 0.5, 0.5, 0.5])
    thr, rule = choose_threshold(y, scores, target_recall=0.99, max_fpr=0.0001)
    assert 0.0 <= thr <= 1.0
    assert "f1" in rule or "recall" in rule


def test_operating_points_are_monotonic():
    rng = np.random.default_rng(1)
    y = np.concatenate([np.ones(1000), np.zeros(4000)]).astype(int)
    scores = np.concatenate([rng.normal(0.8, 0.2, 1000), rng.normal(0.2, 0.2, 4000)])
    table = operating_points(y, np.clip(scores, 0, 1))
    rates = [v["detection_rate"] for v in table["detection_at_fpr"].values()]
    assert rates == sorted(rates), "allowing more false positives cannot detect less"
    fprs = [v["fpr"] for v in table["fpr_at_detection"].values()]
    assert fprs == sorted(fprs)


@pytest.mark.slow
def test_training_writes_usable_artifacts(tmp_path):
    meta = train(parse_args([
        "--n", "40000", "--model", "mlp", "--out", str(tmp_path), "--log-level", "WARNING",
    ]))
    assert (tmp_path / "model.joblib").exists()
    assert (tmp_path / "metadata.json").exists()
    saved = json.loads((tmp_path / "metadata.json").read_text())
    assert saved["model"] == "mlp"
    assert 0.0 < saved["threshold"] < 1.0
    assert saved["metrics"]["roc_auc"] > 0.9
    assert len(saved["feature_names"]) == saved["n_features"]
    assert "operating_points" in saved and "per_class" in saved

    from model.infer import InferenceEngine

    engine = InferenceEngine(model_dir=tmp_path)
    assert engine.meta.threshold == meta["threshold"]
