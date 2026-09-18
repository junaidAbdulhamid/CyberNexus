"""Flow-feature extraction.

The extractor is fully vectorised: a batch of connection records becomes one
float64 matrix and every feature is a numpy expression over whole columns.  A
1000-record batch costs a handful of array operations instead of 1000 Python
loop iterations, which is what makes five-figure connection rates reachable in
CPython.

The feature set follows the Stage 1 brief: duration, byte/packet volumes and
rates, TCP flag rates, inter-arrival statistics, packet-size statistics and
entropy, port characteristics and light TLS/HTTP metadata.  No payload bytes are
required for any feature (see "Security & privacy" in the README): the entropy
feature is the Shannon entropy of the flow's *packet-size distribution*, not of
payload content.
"""
from __future__ import annotations

from typing import Sequence

import numpy as np

from .records import NUM_INDEX, NUMERIC_START, N_NUMERIC

FEATURE_NAMES: tuple[str, ...] = (
    "duration",
    "log_total_bytes",
    "log_orig_bytes",
    "log_resp_bytes",
    "log_total_pkts",
    "log_orig_pkts",
    "log_resp_pkts",
    "bytes_per_pkt",
    "log_bytes_per_sec",
    "log_pkts_per_sec",
    "resp_orig_byte_ratio",
    "resp_orig_pkt_ratio",
    "syn_rate",
    "ack_rate",
    "fin_rate",
    "rst_rate",
    "psh_rate",
    "urg_rate",
    "iat_mean",
    "iat_std",
    "iat_min",
    "iat_max",
    "iat_cv",
    "pkt_size_mean",
    "pkt_size_std",
    "pkt_size_min",
    "pkt_size_max",
    "pkt_size_range",
    "payload_entropy",
    "is_tcp",
    "is_udp",
    "is_icmp",
    "dst_port_wellknown",
    "dst_port_registered",
    "dst_port_ephemeral",
    "log_dst_port",
    "src_port_ephemeral",
    "is_http_port",
    "is_https_port",
    "is_ssh_port",
    "is_dns_port",
    "is_rdp_port",
    "is_smb_port",
    "tls_present",
    "log_tls_sni_len",
    "http_present",
    "log_http_req_count",
)

N_FEATURES = len(FEATURE_NAMES)
FEATURE_INDEX = {name: i for i, name in enumerate(FEATURE_NAMES)}

_EPS = 1e-9


def numeric_matrix(records: Sequence[Sequence]) -> np.ndarray:
    """Slice the numeric tail off a batch of record tuples -> (N, N_NUMERIC)."""
    if not records:
        return np.empty((0, N_NUMERIC), dtype=np.float64)
    mat = np.asarray([r[NUMERIC_START:] for r in records], dtype=np.float64)
    if mat.shape[1] != N_NUMERIC:
        raise ValueError(f"expected {N_NUMERIC} numeric fields, got {mat.shape[1]}")
    return mat


def _col(mat: np.ndarray, name: str) -> np.ndarray:
    return mat[:, NUM_INDEX[name]]


def extract_matrix(mat: np.ndarray) -> np.ndarray:
    """Feature matrix from an already-sliced numeric matrix."""
    n = mat.shape[0]
    out = np.empty((n, N_FEATURES), dtype=np.float32)
    if n == 0:
        return out

    duration = np.maximum(_col(mat, "duration"), 0.0)
    orig_bytes = np.maximum(_col(mat, "orig_bytes"), 0.0)
    resp_bytes = np.maximum(_col(mat, "resp_bytes"), 0.0)
    orig_pkts = np.maximum(_col(mat, "orig_pkts"), 0.0)
    resp_pkts = np.maximum(_col(mat, "resp_pkts"), 0.0)
    total_bytes = orig_bytes + resp_bytes
    total_pkts = orig_pkts + resp_pkts
    pkt_denom = np.maximum(total_pkts, 1.0)
    dur_denom = np.maximum(duration, 1e-3)  # sub-millisecond flows -> 1 ms floor
    protocol = _col(mat, "protocol")
    dst_port = np.clip(_col(mat, "dst_port"), 0, 65535)
    src_port = np.clip(_col(mat, "src_port"), 0, 65535)
    iat_mean = np.maximum(_col(mat, "iat_mean"), 0.0)
    iat_std = np.maximum(_col(mat, "iat_std"), 0.0)

    f = FEATURE_INDEX
    out[:, f["duration"]] = duration
    out[:, f["log_total_bytes"]] = np.log1p(total_bytes)
    out[:, f["log_orig_bytes"]] = np.log1p(orig_bytes)
    out[:, f["log_resp_bytes"]] = np.log1p(resp_bytes)
    out[:, f["log_total_pkts"]] = np.log1p(total_pkts)
    out[:, f["log_orig_pkts"]] = np.log1p(orig_pkts)
    out[:, f["log_resp_pkts"]] = np.log1p(resp_pkts)
    out[:, f["bytes_per_pkt"]] = total_bytes / pkt_denom
    out[:, f["log_bytes_per_sec"]] = np.log1p(total_bytes / dur_denom)
    out[:, f["log_pkts_per_sec"]] = np.log1p(total_pkts / dur_denom)
    # Asymmetry: scanners and exfiltration sit at opposite ends of these.
    out[:, f["resp_orig_byte_ratio"]] = resp_bytes / (orig_bytes + 1.0)
    out[:, f["resp_orig_pkt_ratio"]] = resp_pkts / (orig_pkts + 1.0)

    for flag in ("syn", "ack", "fin", "rst", "psh", "urg"):
        out[:, f[f"{flag}_rate"]] = _col(mat, f"{flag}_count") / pkt_denom

    out[:, f["iat_mean"]] = iat_mean
    out[:, f["iat_std"]] = iat_std
    out[:, f["iat_min"]] = np.maximum(_col(mat, "iat_min"), 0.0)
    out[:, f["iat_max"]] = np.maximum(_col(mat, "iat_max"), 0.0)
    # Coefficient of variation ~0 means metronomic timing: the C2 beacon tell.
    out[:, f["iat_cv"]] = iat_std / (iat_mean + _EPS)

    pkt_min = np.maximum(_col(mat, "pkt_size_min"), 0.0)
    pkt_max = np.maximum(_col(mat, "pkt_size_max"), 0.0)
    out[:, f["pkt_size_mean"]] = _col(mat, "pkt_size_mean")
    out[:, f["pkt_size_std"]] = _col(mat, "pkt_size_std")
    out[:, f["pkt_size_min"]] = pkt_min
    out[:, f["pkt_size_max"]] = pkt_max
    out[:, f["pkt_size_range"]] = np.maximum(pkt_max - pkt_min, 0.0)
    out[:, f["payload_entropy"]] = _col(mat, "payload_entropy")

    out[:, f["is_tcp"]] = protocol == 6
    out[:, f["is_udp"]] = protocol == 17
    out[:, f["is_icmp"]] = protocol == 1

    out[:, f["dst_port_wellknown"]] = dst_port < 1024
    out[:, f["dst_port_registered"]] = (dst_port >= 1024) & (dst_port < 49152)
    out[:, f["dst_port_ephemeral"]] = dst_port >= 49152
    out[:, f["log_dst_port"]] = np.log1p(dst_port)
    out[:, f["src_port_ephemeral"]] = src_port >= 49152
    out[:, f["is_http_port"]] = (dst_port == 80) | (dst_port == 8080)
    out[:, f["is_https_port"]] = dst_port == 443
    out[:, f["is_ssh_port"]] = dst_port == 22
    out[:, f["is_dns_port"]] = dst_port == 53
    out[:, f["is_rdp_port"]] = dst_port == 3389
    out[:, f["is_smb_port"]] = (dst_port == 445) | (dst_port == 139)

    out[:, f["tls_present"]] = _col(mat, "tls_present") > 0
    out[:, f["log_tls_sni_len"]] = np.log1p(np.maximum(_col(mat, "tls_sni_len"), 0.0))
    out[:, f["http_present"]] = _col(mat, "http_present") > 0
    out[:, f["log_http_req_count"]] = np.log1p(np.maximum(_col(mat, "http_req_count"), 0.0))

    # Cheap insurance: a single NaN from malformed capture data would otherwise
    # poison a whole inference batch.
    np.nan_to_num(out, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
    return out


def extract_batch(records: Sequence[Sequence]) -> np.ndarray:
    """Feature matrix (N, N_FEATURES) float32 from a batch of record tuples."""
    return extract_matrix(numeric_matrix(records))


def extract_one(record: Sequence) -> np.ndarray:
    """Single-record convenience wrapper (tests, debugging, REST scoring)."""
    return extract_batch([record])[0]


def labels_of(records: Sequence[Sequence]) -> np.ndarray:
    """Ground-truth labels carried by synthetic records (-1 when unknown)."""
    return numeric_matrix(records)[:, NUM_INDEX["label"]]


def size_entropy(sizes: Sequence[float], bin_width: int = 64) -> float:
    """Shannon entropy (bits) of a flow's packet-size distribution.

    Packet sizes are bucketed into ``bin_width``-byte bins before the entropy is
    taken, so the value is stable for flows with only a handful of packets and
    never requires inspecting payload bytes.
    """
    if not len(sizes):
        return 0.0
    arr = np.asarray(sizes, dtype=np.float64)
    bins = np.floor(np.clip(arr, 0, 65535) / bin_width).astype(np.int64)
    _, counts = np.unique(bins, return_counts=True)
    p = counts / counts.sum()
    return float(max(0.0, -np.sum(p * np.log2(p))))
