"""The Stage 1 alert wire format, declared once.

Stage 1 publishes msgpack-encoded *batches* of alert dicts to a Redis Stream
(``cn:alerts``), one field per entry, keyed ``b"b"``.  Stage 2 could import
``cybernexus.wire`` from the Stage 1 tree, but then this service could not be
deployed without a copy of Stage 1 on the PYTHONPATH — so the format is declared
here instead, in the eleven lines it actually takes.

The duplication is deliberate and it is guarded:
``tests/test_stage1_compat.py`` round-trips this decoder against Stage 1's real
encoder whenever Stage 1 is importable, and fails loudly if the two ever drift.
"""
from __future__ import annotations

from typing import Any, Iterable

import msgpack

from cnmap.models import Alert

#: Field name inside each Redis Stream entry (matches Stage 1's ``bus.FIELD``).
STREAM_FIELD = b"b"

#: Keys Stage 1 puts in an alert dict.
ALERT_KEYS = (
    "flow_id", "src_ip", "dst_ip", "src_port", "dst_port", "protocol",
    "score", "threshold", "detected_at", "latency_s", "label",
)


def unpack_alert_batch(blob: bytes) -> list[dict]:
    """Decode one stream entry into a list of raw alert dicts."""
    data = msgpack.unpackb(blob, raw=False, use_list=True, strict_map_key=False)
    if isinstance(data, dict):
        return [data]
    return list(data)


def pack_alert_batch(alerts: Iterable[dict]) -> bytes:
    """Encode alerts the way Stage 1 does (used by the simulator and tests)."""
    return msgpack.packb(list(alerts), use_bin_type=True)


def to_alert(raw: dict[str, Any]) -> Alert:
    """Raw Stage 1 dict -> Stage 2 ``Alert``.

    Unknown keys are ignored rather than rejected: Stage 1 adding a field must
    not take the map offline.
    """
    return Alert(
        flow_id=str(raw.get("flow_id", "")),
        src_ip=str(raw.get("src_ip", "")),
        dst_ip=str(raw.get("dst_ip", "")),
        src_port=int(raw.get("src_port", 0) or 0),
        dst_port=int(raw.get("dst_port", 0) or 0),
        protocol=int(raw.get("protocol", 6) or 6),
        score=float(raw.get("score", 0.0) or 0.0),
        threshold=float(raw.get("threshold", 0.0) or 0.0),
        detected_at=float(raw.get("detected_at", 0.0) or 0.0),
        latency_s=float(raw.get("latency_s", 0.0) or 0.0),
        label=float(raw.get("label", -1.0)),
    )


def decode_entry(blob: bytes) -> list[Alert]:
    return [to_alert(raw) for raw in unpack_alert_batch(blob)]
