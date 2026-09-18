import numpy as np
import pytest

from cybernexus.features import extract_matrix
from cybernexus.records import NUM_INDEX, N_FIELDS
from cybernexus.trafficmodel import (
    ATTACK_FRACTION, CLASS_NAMES, DEFAULT_MIX, IS_ATTACK, TrafficModel, class_report,
)


def test_mix_is_a_distribution():
    assert sum(DEFAULT_MIX.values()) == pytest.approx(1.0)
    assert set(DEFAULT_MIX) == set(CLASS_NAMES)
    assert 0.05 < ATTACK_FRACTION < 0.3


def test_sample_matrix_shape_and_labels():
    mat, cids = TrafficModel(seed=1).sample_matrix(5000)
    assert mat.shape == (5000, len(NUM_INDEX))
    assert set(np.unique(mat[:, NUM_INDEX["label"]])) <= {0.0, 1.0}
    assert np.array_equal(mat[:, NUM_INDEX["label"]], IS_ATTACK[cids])


def test_attack_fraction_is_close_to_nominal():
    mat, _ = TrafficModel(seed=2).sample_matrix(50_000)
    observed = mat[:, NUM_INDEX["label"]].mean()
    assert observed == pytest.approx(ATTACK_FRACTION, abs=0.02)


def test_same_seed_is_reproducible():
    a, ca = TrafficModel(seed=7).sample_matrix(1000)
    b, cb = TrafficModel(seed=7).sample_matrix(1000)
    assert np.array_equal(a, b) and np.array_equal(ca, cb)


def test_different_seeds_differ():
    a, _ = TrafficModel(seed=7).sample_matrix(1000)
    b, _ = TrafficModel(seed=8).sample_matrix(1000)
    assert not np.array_equal(a, b)


def test_records_have_full_width_and_ids():
    recs, cids = TrafficModel(seed=3).sample_records(500)
    assert len(recs) == 500 == len(cids)
    assert all(len(r) == N_FIELDS for r in recs)
    assert len({r[0] for r in recs}) == 500          # flow ids are unique
    assert all(isinstance(r[1], str) and isinstance(r[2], str) for r in recs)


def test_no_nan_or_negative_volumes():
    mat, _ = TrafficModel(seed=4).sample_matrix(20_000)
    assert np.isfinite(mat).all()
    for name in ("orig_bytes", "resp_bytes", "orig_pkts", "resp_pkts", "duration"):
        assert (mat[:, NUM_INDEX[name]] >= 0).all()


def test_packet_size_invariant_holds():
    # bytes / packets must agree with the reported mean size, or the model
    # could learn an artefact that does not exist in captured traffic.
    mat, _ = TrafficModel(seed=5).sample_matrix(20_000)
    total_b = mat[:, NUM_INDEX["orig_bytes"]] + mat[:, NUM_INDEX["resp_bytes"]]
    total_p = np.maximum(mat[:, NUM_INDEX["orig_pkts"]] + mat[:, NUM_INDEX["resp_pkts"]], 1)
    expected = np.clip(total_b / total_p, 40.0, 1500.0)
    assert np.allclose(mat[:, NUM_INDEX["pkt_size_mean"]], expected, rtol=1e-6)


def test_udp_flows_have_no_tcp_flags():
    mat, _ = TrafficModel(seed=6).sample_matrix(20_000)
    udp = mat[:, NUM_INDEX["protocol"]] == 17
    assert udp.sum() > 0
    for name in ("syn_count", "ack_count", "fin_count", "rst_count"):
        assert (mat[udp, NUM_INDEX[name]] == 0).all()


def test_features_are_finite_for_every_class():
    mat, _ = TrafficModel(seed=9).sample_matrix(20_000)
    assert np.isfinite(extract_matrix(mat)).all()


def test_hardness_zero_separates_more_than_default():
    """The ambiguity mechanism must actually make the problem harder."""
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import train_test_split

    def auc(hardness):
        mat, _ = TrafficModel(seed=11, hardness=hardness).sample_matrix(30_000)
        X, y = extract_matrix(mat), mat[:, NUM_INDEX["label"]].astype(int)
        Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.3, random_state=0, stratify=y)
        clf = RandomForestClassifier(n_estimators=40, n_jobs=-1, random_state=0).fit(Xtr, ytr)
        return roc_auc_score(yte, clf.predict_proba(Xte)[:, 1])

    assert auc(0.0) > auc(0.15)


def test_drift_shifts_the_distribution():
    base, _ = TrafficModel(seed=12, drift=0.0).sample_matrix(20_000)
    drifted, _ = TrafficModel(seed=12, drift=0.5).sample_matrix(20_000)
    col = NUM_INDEX["orig_bytes"]
    assert np.std(np.log1p(drifted[:, col])) > np.std(np.log1p(base[:, col]))


def test_class_report_counts():
    scan = CLASS_NAMES.index("attack_port_scan")
    class_ids = np.array([0, 0, scan, scan, scan])
    flagged = np.array([0, 1, 1, 1, 0])
    report = class_report(class_ids, flagged)
    assert report[CLASS_NAMES[0]]["count"] == 2
    assert report[CLASS_NAMES[0]]["is_attack"] is False
    assert report["attack_port_scan"]["flagged"] == 2
    assert report["attack_port_scan"]["is_attack"] is True


def test_custom_mix_is_normalised():
    model = TrafficModel(seed=1, mix={"attack_port_scan": 1.0})
    mat, cids = model.sample_matrix(500)
    assert (mat[:, NUM_INDEX["label"]] == 1.0).all()
    assert set(np.unique(cids)) == {CLASS_NAMES.index("attack_port_scan")}
