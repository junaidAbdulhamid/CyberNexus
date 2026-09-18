"""Wire formats for the feature and alert stages.

Feature vectors are moved as one raw float32 block per batch plus a parallel
list of identifiers, not as a list of per-flow dicts.  A 512-flow batch is then
a single ~100 KB message that the scorer turns back into a matrix with
``np.frombuffer`` — zero per-row parsing, and the matrix is already in the
layout ONNX Runtime wants.
"""
from __future__ import annotations

from typing import Any, Sequence

import msgpack
import numpy as np

FEATURE_DTYPE = np.float32


def pack_features(
    flow_ids: Sequence[str],
    meta: Sequence[Sequence[Any]],
    X: np.ndarray,
    labels: np.ndarray | None = None,
    emit_ts: np.ndarray | None = None,
) -> bytes:
    """``meta`` rows are ``(src_ip, dst_ip, src_port, dst_port, protocol)``."""
    X = np.ascontiguousarray(X, dtype=FEATURE_DTYPE)
    payload = {
        "ids": list(flow_ids),
        "meta": [list(m) for m in meta],
        "n": int(X.shape[0]),
        "f": int(X.shape[1]),
        "x": X.tobytes(),
        "y": np.ascontiguousarray(labels, dtype=np.float32).tobytes() if labels is not None else None,
        "t": np.ascontiguousarray(emit_ts, dtype=np.float64).tobytes() if emit_ts is not None else None,
    }
    return msgpack.packb(payload, use_bin_type=True)


def unpack_features(blob: bytes) -> dict:
    d = msgpack.unpackb(blob, raw=False, use_list=True, strict_map_key=False)
    n, f = d["n"], d["f"]
    d["x"] = np.frombuffer(d["x"], dtype=FEATURE_DTYPE).reshape(n, f)
    d["y"] = np.frombuffer(d["y"], dtype=np.float32) if d.get("y") else None
    d["t"] = np.frombuffer(d["t"], dtype=np.float64) if d.get("t") else None
    return d


def pack_alerts(alerts: Sequence[dict]) -> bytes:
    return msgpack.packb(list(alerts), use_bin_type=True)


def unpack_alerts(blob: bytes) -> list[dict]:
    return msgpack.unpackb(blob, raw=False, use_list=True, strict_map_key=False)


def pack_scores(ids: Sequence[str], scores: np.ndarray, labels: np.ndarray | None,
                latency: np.ndarray | None) -> bytes:
    """Every scored flow, for offline ROC computation in the bench harness."""
    return msgpack.packb({
        "ids": list(ids),
        "s": np.ascontiguousarray(scores, dtype=np.float32).tobytes(),
        "y": np.ascontiguousarray(labels, dtype=np.float32).tobytes() if labels is not None else None,
        "l": np.ascontiguousarray(latency, dtype=np.float32).tobytes() if latency is not None else None,
    }, use_bin_type=True)


def unpack_scores(blob: bytes) -> dict:
    d = msgpack.unpackb(blob, raw=False, use_list=True, strict_map_key=False)
    d["s"] = np.frombuffer(d["s"], dtype=np.float32)
    d["y"] = np.frombuffer(d["y"], dtype=np.float32) if d.get("y") else None
    d["l"] = np.frombuffer(d["l"], dtype=np.float32) if d.get("l") else None
    return d
