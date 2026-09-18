"""Dataset construction for training and offline evaluation.

Three sources, one feature pipeline (:mod:`cybernexus.features`):

``synthetic``
    The default.  Draws from :mod:`cybernexus.trafficmodel`, which is also what
    the benchmark replays, so training data and bench traffic come from the same
    family of distributions but never the same seed.

``unsw``
    UNSW-NB15 (Moustafa & Slay, 2015).  The four ``UNSW-NB15_[1-4].csv`` part
    files or the ``UNSW_NB15_training-set.csv`` / ``testing-set.csv`` split.

``cic``
    CIC-IDS-2017 (Sharafaldin et al., 2018), the ``MachineLearningCSV``
    per-day flow exports.

Both public datasets are *flow* datasets, so each row maps onto a
``ConnectionRecord`` and then through the identical extractor the live pipeline
uses.  Fields those datasets do not carry (TLS SNI length, HTTP request counts)
are left at zero rather than invented; the features derived from them simply
carry no signal for those runs.  See ``model/get_dataset.sh`` for where to get
the files.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from cybernexus.features import FEATURE_NAMES, extract_matrix
from cybernexus.records import NUM_INDEX, N_NUMERIC
from cybernexus.trafficmodel import CLASS_NAMES, TrafficModel

log = logging.getLogger(__name__)


@dataclass
class Dataset:
    X: np.ndarray            # (n, n_features) float32
    y: np.ndarray            # (n,) 0/1
    class_ids: np.ndarray    # (n,) index into class_names, -1 when unknown
    class_names: tuple[str, ...]
    source: str

    def __len__(self) -> int:  # pragma: no cover - trivial
        return self.X.shape[0]

    def summary(self) -> str:
        pos = int(self.y.sum())
        return (
            f"{self.source}: {len(self):,} flows, {pos:,} attack "
            f"({pos / max(len(self), 1):.1%}), {self.X.shape[1]} features"
        )


def build_synthetic(n: int = 400_000, seed: int = 20240917, drift: float = 0.0) -> Dataset:
    model = TrafficModel(seed=seed, drift=drift)
    chunks_X, chunks_y, chunks_c = [], [], []
    remaining = n
    # Chunked so peak memory stays flat for multi-million-row runs.
    while remaining > 0:
        take = min(remaining, 100_000)
        mat, cids = model.sample_matrix(take)
        chunks_X.append(extract_matrix(mat))
        chunks_y.append(mat[:, NUM_INDEX["label"]].astype(np.int8))
        chunks_c.append(cids)
        remaining -= take
    return Dataset(
        X=np.vstack(chunks_X),
        y=np.concatenate(chunks_y),
        class_ids=np.concatenate(chunks_c),
        class_names=CLASS_NAMES,
        source=f"synthetic(seed={seed}, drift={drift})",
    )


# ---------------------------------------------------------------------------
# Public dataset adapters
# ---------------------------------------------------------------------------

_PROTO_NUM = {"tcp": 6, "udp": 17, "icmp": 1, "igmp": 2, "sctp": 132}


def _blank_matrix(n: int) -> np.ndarray:
    return np.zeros((n, N_NUMERIC), dtype=np.float64)


def _set(mat: np.ndarray, name: str, values) -> None:
    mat[:, NUM_INDEX[name]] = np.nan_to_num(np.asarray(values, dtype=np.float64))


def _find_files(path: Path, patterns: tuple[str, ...]) -> list[Path]:
    """Files matching any pattern, each listed once (the patterns overlap)."""
    if path.is_file():
        return [path]
    seen: dict[Path, None] = {}
    for pattern in patterns:
        for f in sorted(path.glob(pattern)):
            seen.setdefault(f.resolve(), None)
    return list(seen)


def load_unsw(path: str | Path) -> Dataset:
    """Load UNSW-NB15 flow CSVs into the CyberNexus feature space."""
    import pandas as pd

    path = Path(path)
    files = _find_files(path, ("UNSW*.csv", "*.csv"))
    if not files:
        raise FileNotFoundError(f"no UNSW-NB15 CSVs under {path}")
    frames = [pd.read_csv(f, low_memory=False) for f in files]
    df = pd.concat(frames, ignore_index=True)
    df.columns = [c.strip().lower() for c in df.columns]
    log.info("UNSW-NB15: %d rows from %d file(s)", len(df), len(files))

    n = len(df)
    mat = _blank_matrix(n)
    col = lambda name, default=0.0: (  # noqa: E731 - terse on purpose
        df[name].to_numpy() if name in df.columns else np.full(n, default)
    )

    proto = df["proto"].astype(str).str.lower() if "proto" in df.columns else None
    _set(mat, "protocol", proto.map(_PROTO_NUM).fillna(0).to_numpy() if proto is not None else 6.0)
    _set(mat, "src_port", col("sport"))
    _set(mat, "dst_port", col("dsport") if "dsport" in df.columns else col("dport"))
    _set(mat, "duration", col("dur"))
    _set(mat, "orig_bytes", col("sbytes"))
    _set(mat, "resp_bytes", col("dbytes"))
    _set(mat, "orig_pkts", col("spkts"))
    _set(mat, "resp_pkts", col("dpkts"))
    # UNSW reports mean inter-packet time in milliseconds, per direction.
    sinpkt, dinpkt = col("sinpkt") / 1000.0, col("dinpkt") / 1000.0
    iat_mean = np.where(dinpkt > 0, (sinpkt + dinpkt) / 2.0, sinpkt)
    _set(mat, "iat_mean", iat_mean)
    _set(mat, "iat_std", np.abs(sinpkt - dinpkt) / 2.0)
    _set(mat, "iat_min", np.minimum(sinpkt, np.where(dinpkt > 0, dinpkt, sinpkt)))
    _set(mat, "iat_max", np.maximum(sinpkt, dinpkt))
    smean, dmean = col("smean"), col("dmean")
    _set(mat, "pkt_size_mean", np.where((smean + dmean) > 0, (smean + dmean) / 2.0, smean))
    _set(mat, "pkt_size_std", np.abs(smean - dmean) / 2.0)
    _set(mat, "pkt_size_min", np.minimum(smean, np.where(dmean > 0, dmean, smean)))
    _set(mat, "pkt_size_max", np.maximum(smean, dmean))
    # Flag counts are not itemised; state/ct_* columns stand in for the shape.
    state = df["state"].astype(str).str.upper() if "state" in df.columns else None
    if state is not None:
        _set(mat, "syn_count", (state.isin(["INT", "REQ", "CON", "FIN"])).to_numpy())
        _set(mat, "rst_count", (state.isin(["RST", "CLO"])).to_numpy())
        _set(mat, "fin_count", (state == "FIN").to_numpy())
    service = df["service"].astype(str).str.lower() if "service" in df.columns else None
    if service is not None:
        _set(mat, "http_present", service.str.contains("http").to_numpy())
        _set(mat, "tls_present", service.str.contains("ssl|tls").to_numpy())

    label_col = "label" if "label" in df.columns else "attack_cat"
    y = (
        df["label"].to_numpy().astype(np.int8)
        if label_col == "label"
        else (~df["attack_cat"].isna()).to_numpy().astype(np.int8)
    )
    if "attack_cat" in df.columns:
        cats = df["attack_cat"].fillna("Normal").astype(str).str.strip()
        names = tuple(sorted(cats.unique()))
        index = {name: i for i, name in enumerate(names)}
        class_ids = cats.map(index).to_numpy().astype(np.int16)
    else:
        names, class_ids = ("normal", "attack"), y.astype(np.int16)

    X = extract_matrix(mat)
    return Dataset(X=X, y=y, class_ids=class_ids, class_names=names, source=f"unsw-nb15({path})")


def load_cic(path: str | Path) -> Dataset:
    """Load CIC-IDS-2017 ``MachineLearningCSV`` exports."""
    import pandas as pd

    path = Path(path)
    files = _find_files(path, ("*.pcap_ISCX.csv", "*.csv"))
    if not files:
        raise FileNotFoundError(f"no CIC-IDS CSVs under {path}")
    frames = []
    for f in files:
        d = pd.read_csv(f, low_memory=False, encoding="latin-1")
        d.columns = [c.strip().lower().replace("/", "_").replace(" ", "_") for c in d.columns]
        frames.append(d)
    df = pd.concat(frames, ignore_index=True)
    df = df.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    log.info("CIC-IDS-2017: %d rows from %d file(s)", len(df), len(files))

    n = len(df)
    mat = _blank_matrix(n)
    col = lambda name, default=0.0: (  # noqa: E731
        pd.to_numeric(df[name], errors="coerce").fillna(default).to_numpy()
        if name in df.columns
        else np.full(n, default)
    )
    _set(mat, "protocol", col("protocol", 6.0))
    _set(mat, "dst_port", col("destination_port"))
    _set(mat, "src_port", col("source_port", 50000.0))
    _set(mat, "duration", col("flow_duration") / 1e6)  # microseconds
    _set(mat, "orig_bytes", col("total_length_of_fwd_packets"))
    _set(mat, "resp_bytes", col("total_length_of_bwd_packets"))
    _set(mat, "orig_pkts", col("total_fwd_packets"))
    _set(mat, "resp_pkts", col("total_backward_packets"))
    _set(mat, "iat_mean", col("flow_iat_mean") / 1e6)
    _set(mat, "iat_std", col("flow_iat_std") / 1e6)
    _set(mat, "iat_min", col("flow_iat_min") / 1e6)
    _set(mat, "iat_max", col("flow_iat_max") / 1e6)
    _set(mat, "pkt_size_mean", col("average_packet_size"))
    _set(mat, "pkt_size_std", col("packet_length_std"))
    _set(mat, "pkt_size_min", col("min_packet_length"))
    _set(mat, "pkt_size_max", col("max_packet_length"))
    for flag, column in (
        ("syn_count", "syn_flag_count"), ("ack_count", "ack_flag_count"),
        ("fin_count", "fin_flag_count"), ("rst_count", "rst_flag_count"),
        ("psh_count", "psh_flag_count"), ("urg_count", "urg_flag_count"),
    ):
        _set(mat, flag, col(column))

    labels = df["label"].astype(str).str.strip() if "label" in df.columns else None
    if labels is None:
        raise ValueError("CIC-IDS CSV has no Label column")
    y = (labels.str.upper() != "BENIGN").to_numpy().astype(np.int8)
    names = tuple(sorted(labels.unique()))
    index = {name: i for i, name in enumerate(names)}
    class_ids = labels.map(index).to_numpy().astype(np.int16)
    return Dataset(X=extract_matrix(mat), y=y, class_ids=class_ids, class_names=names,
                   source=f"cic-ids-2017({path})")


def load_dataset(kind: str, path: str | Path | None = None, **kwargs) -> Dataset:
    kind = kind.lower()
    if kind == "synthetic":
        return build_synthetic(**kwargs)
    if kind == "unsw":
        if not path:
            raise ValueError("--data PATH is required for --dataset unsw")
        return load_unsw(path)
    if kind == "cic":
        if not path:
            raise ValueError("--data PATH is required for --dataset cic")
        return load_cic(path)
    raise ValueError(f"unknown dataset {kind!r}")


__all__ = ["Dataset", "build_synthetic", "load_unsw", "load_cic", "load_dataset", "FEATURE_NAMES"]
