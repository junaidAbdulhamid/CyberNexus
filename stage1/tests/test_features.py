import numpy as np
import pytest

from cybernexus.features import (
    FEATURE_INDEX, FEATURE_NAMES, N_FEATURES, extract_batch, extract_one,
    labels_of, numeric_matrix, size_entropy,
)
from cybernexus.records import ConnectionRecord


def make(**kw):
    return ConnectionRecord("f1", "10.0.0.1", "203.0.113.5", **kw).to_tuple()


def test_shape_and_dtype():
    X = extract_batch([make(), make()])
    assert X.shape == (2, N_FEATURES) == (2, len(FEATURE_NAMES))
    assert X.dtype == np.float32


def test_empty_batch():
    X = extract_batch([])
    assert X.shape == (0, N_FEATURES)


def test_known_values():
    rec = make(src_port=51000, dst_port=443, protocol=6, duration=2.0,
               orig_bytes=1000, resp_bytes=3000, orig_pkts=10, resp_pkts=30,
               syn_count=1, ack_count=38, iat_mean=0.05, iat_std=0.01,
               pkt_size_mean=100, pkt_size_std=20, pkt_size_min=40, pkt_size_max=1500,
               tls_present=1, tls_sni_len=15)
    f = extract_one(rec)
    g = FEATURE_INDEX
    assert f[g["duration"]] == pytest.approx(2.0)
    assert f[g["log_total_bytes"]] == pytest.approx(np.log1p(4000), rel=1e-5)
    assert f[g["bytes_per_pkt"]] == pytest.approx(100.0, rel=1e-5)
    assert f[g["resp_orig_byte_ratio"]] == pytest.approx(3000 / 1001, rel=1e-5)
    assert f[g["syn_rate"]] == pytest.approx(1 / 40, rel=1e-5)
    assert f[g["iat_cv"]] == pytest.approx(0.2, rel=1e-4)
    assert f[g["pkt_size_range"]] == pytest.approx(1460.0)
    assert f[g["is_tcp"]] == 1.0 and f[g["is_udp"]] == 0.0
    assert f[g["is_https_port"]] == 1.0 and f[g["dst_port_wellknown"]] == 1.0
    assert f[g["src_port_ephemeral"]] == 1.0
    assert f[g["tls_present"]] == 1.0


def test_zero_duration_and_zero_packets_do_not_divide_by_zero():
    f = extract_one(make(duration=0.0, orig_pkts=0, resp_pkts=0, orig_bytes=0, resp_bytes=0))
    assert np.all(np.isfinite(f))


def test_nan_and_inf_are_scrubbed():
    rec = list(make(duration=float("nan"), orig_bytes=float("inf")))
    X = extract_batch([tuple(rec)])
    assert np.all(np.isfinite(X))


def test_label_does_not_leak_into_features():
    # Ground truth rides inside the record; the extractor must never read it.
    benign = extract_one(make(duration=1.0, orig_bytes=500, orig_pkts=5, label=0.0))
    attack = extract_one(make(duration=1.0, orig_bytes=500, orig_pkts=5, label=1.0))
    assert np.array_equal(benign, attack)


def test_batch_matches_single_record():
    recs = [make(orig_bytes=b, duration=b / 100.0, orig_pkts=b // 50 + 1) for b in (100, 5000, 90000)]
    batch = extract_batch(recs)
    for i, rec in enumerate(recs):
        assert np.allclose(batch[i], extract_one(rec))


def test_udp_has_no_tcp_flag_rates():
    f = extract_one(make(protocol=17, orig_pkts=2, resp_pkts=1))
    assert f[FEATURE_INDEX["is_udp"]] == 1.0
    assert f[FEATURE_INDEX["syn_rate"]] == 0.0


def test_ports_out_of_range_are_clipped():
    f = extract_one(make(dst_port=999999, src_port=-5))
    assert np.all(np.isfinite(f))
    assert f[FEATURE_INDEX["dst_port_ephemeral"]] == 1.0


def test_labels_of():
    assert list(labels_of([make(label=1.0), make(label=0.0)])) == [1.0, 0.0]


def test_numeric_matrix_width():
    mat = numeric_matrix([make(), make()])
    assert mat.shape[0] == 2
    assert mat.shape[1] == len(ConnectionRecord.__dataclass_fields__) - 3


@pytest.mark.parametrize("sizes,expected", [
    ([], 0.0),
    ([100] * 10, 0.0),                 # one bucket -> no uncertainty
    ([40, 40, 1500, 1500], 1.0),       # two equally likely buckets -> 1 bit
])
def test_size_entropy(sizes, expected):
    assert size_entropy(sizes) == pytest.approx(expected, abs=1e-9)


def test_size_entropy_increases_with_spread():
    narrow = size_entropy([500, 510, 520, 530])
    wide = size_entropy([40, 400, 800, 1500])
    assert wide > narrow


def test_feature_names_unique():
    assert len(set(FEATURE_NAMES)) == len(FEATURE_NAMES)
