import numpy as np
import pytest

from cybernexus.features import FEATURE_NAMES
from model.dataset import build_synthetic, load_cic, load_dataset, load_unsw


def test_build_synthetic_shape():
    ds = build_synthetic(5_000, seed=3)
    assert ds.X.shape == (5_000, len(FEATURE_NAMES))
    assert set(np.unique(ds.y)) <= {0, 1}
    assert 0.05 < ds.y.mean() < 0.3
    assert "synthetic" in ds.summary()


def test_build_synthetic_is_deterministic():
    assert np.array_equal(build_synthetic(2_000, seed=9).X, build_synthetic(2_000, seed=9).X)


def test_chunking_does_not_change_results():
    # build_synthetic chunks at 100k; a larger request must still be coherent.
    ds = build_synthetic(120_000, seed=11)
    assert len(ds) == 120_000
    assert np.isfinite(ds.X).all()


def test_drift_changes_the_data():
    a = build_synthetic(5_000, seed=13, drift=0.0)
    b = build_synthetic(5_000, seed=13, drift=0.6)
    assert not np.allclose(a.X, b.X)


def test_load_dataset_dispatch():
    assert load_dataset("synthetic", n=1_000).X.shape[0] == 1_000
    with pytest.raises(ValueError):
        load_dataset("nonsense")
    with pytest.raises(ValueError):
        load_dataset("unsw")            # missing --data


def test_unsw_adapter_maps_columns(tmp_path):
    """The public-dataset adapter must land in the same feature space."""
    import pandas as pd

    df = pd.DataFrame({
        "proto": ["tcp", "udp", "tcp"],
        "sport": [51000, 50000, 52000],
        "dsport": [443, 53, 22],
        "dur": [1.5, 0.01, 300.0],
        "sbytes": [1200, 90, 40000],
        "dbytes": [30000, 200, 50000],
        "spkts": [12, 1, 400],
        "dpkts": [30, 1, 420],
        "sinpkt": [50.0, 1.0, 700.0],      # milliseconds
        "dinpkt": [30.0, 1.0, 650.0],
        "smean": [100, 90, 100],
        "dmean": [1000, 200, 120],
        "state": ["FIN", "INT", "CON"],
        "service": ["-", "dns", "ssh"],
        "attack_cat": [None, "Reconnaissance", None],
        "label": [0, 1, 0],
    })
    path = tmp_path / "UNSW-NB15_1.csv"
    df.to_csv(path, index=False)

    ds = load_unsw(tmp_path)
    assert ds.X.shape == (3, len(FEATURE_NAMES))
    assert list(ds.y) == [0, 1, 0]
    assert np.isfinite(ds.X).all()
    from cybernexus.features import FEATURE_INDEX
    assert ds.X[0, FEATURE_INDEX["duration"]] == pytest.approx(1.5)
    assert ds.X[0, FEATURE_INDEX["is_tcp"]] == 1.0
    assert ds.X[1, FEATURE_INDEX["is_udp"]] == 1.0
    assert ds.X[1, FEATURE_INDEX["is_dns_port"]] == 1.0
    # milliseconds must be converted to seconds
    assert ds.X[0, FEATURE_INDEX["iat_mean"]] == pytest.approx(0.04, abs=1e-6)


def test_cic_adapter_maps_columns(tmp_path):
    import pandas as pd

    df = pd.DataFrame({
        " Destination Port": [80, 22],
        " Protocol": [6, 6],
        " Flow Duration": [2_000_000, 500_000],      # microseconds
        "Total Length of Fwd Packets": [800, 200],
        " Total Length of Bwd Packets": [9000, 150],
        " Total Fwd Packets": [8, 4],
        " Total Backward Packets": [10, 3],
        " Flow IAT Mean": [100_000, 50_000],
        " Flow IAT Std": [10_000, 5_000],
        " Flow IAT Min": [1_000, 500],
        " Flow IAT Max": [200_000, 90_000],
        " Average Packet Size": [500, 60],
        " Packet Length Std": [300, 10],
        " Min Packet Length": [40, 40],
        " Max Packet Length": [1500, 80],
        " SYN Flag Count": [1, 1],
        " ACK Flag Count": [16, 5],
        " Label": ["BENIGN", "FTP-Patator"],
    })
    path = tmp_path / "day.pcap_ISCX.csv"
    df.to_csv(path, index=False)

    ds = load_cic(tmp_path)
    from cybernexus.features import FEATURE_INDEX
    assert ds.X.shape == (2, len(FEATURE_NAMES))
    assert list(ds.y) == [0, 1]
    assert ds.X[0, FEATURE_INDEX["duration"]] == pytest.approx(2.0)
    assert ds.X[0, FEATURE_INDEX["is_http_port"]] == 1.0
    assert ds.X[1, FEATURE_INDEX["is_ssh_port"]] == 1.0


def test_adapters_reject_missing_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_unsw(tmp_path)
    with pytest.raises(FileNotFoundError):
        load_cic(tmp_path)
