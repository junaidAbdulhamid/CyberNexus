"""Wire format for connection records.

A connection record is a plain Python *tuple* in a fixed field order rather than
a dict or a dataclass instance.  That choice is deliberate and is the single
biggest reason this pipeline sustains five-figure connection rates in CPython:

* tuples of scalars pack into msgpack ~4x faster and ~3x smaller than dicts with
  string keys, and the keys are pure redundancy once the schema is fixed;
* the numeric fields are laid out contiguously at the tail of the tuple, so an
  entire batch converts to a float64 matrix with one ``np.asarray`` call and the
  feature extractor never touches Python objects per record;
* batches (not records) are the unit of transport, so per-message bus overhead
  is amortised over ``settings.publish_batch`` connections.

``ConnectionRecord`` exists for readability in tests and in the capture path;
it is never used on the hot loop.
"""
from __future__ import annotations

from dataclasses import astuple, dataclass, fields as dc_fields
from typing import Any, Iterable, Sequence

import msgpack

#: Field order on the wire.  Index 0..2 are strings, 3..end are numeric.
FIELDS: tuple[str, ...] = (
    "flow_id",
    "src_ip",
    "dst_ip",
    # --- numeric tail (NUMERIC_START ..) ---------------------------------
    "src_port",
    "dst_port",
    "protocol",
    "start_ts",
    "emit_ts",
    "duration",
    "orig_bytes",
    "resp_bytes",
    "orig_pkts",
    "resp_pkts",
    "syn_count",
    "ack_count",
    "fin_count",
    "rst_count",
    "psh_count",
    "urg_count",
    "iat_mean",
    "iat_std",
    "iat_min",
    "iat_max",
    "pkt_size_mean",
    "pkt_size_std",
    "pkt_size_min",
    "pkt_size_max",
    "payload_entropy",
    "tls_present",
    "tls_sni_len",
    "http_present",
    "http_req_count",
    "label",
)

NUMERIC_START = 3
FIELD_INDEX: dict[str, int] = {name: i for i, name in enumerate(FIELDS)}
#: Column offset inside the numeric matrix produced by ``numeric_matrix``.
NUM_INDEX: dict[str, int] = {name: i - NUMERIC_START for i, name in enumerate(FIELDS) if i >= NUMERIC_START}
N_FIELDS = len(FIELDS)
N_NUMERIC = N_FIELDS - NUMERIC_START

#: ``label`` is ground truth and is only populated by the synthetic generators.
LABEL_UNKNOWN = -1.0


@dataclass(slots=True)
class ConnectionRecord:
    """Readable mirror of the wire tuple (documentation / tests / capture)."""

    flow_id: str
    src_ip: str
    dst_ip: str
    src_port: float = 0.0
    dst_port: float = 0.0
    protocol: float = 6.0
    start_ts: float = 0.0
    emit_ts: float = 0.0
    duration: float = 0.0
    orig_bytes: float = 0.0
    resp_bytes: float = 0.0
    orig_pkts: float = 0.0
    resp_pkts: float = 0.0
    syn_count: float = 0.0
    ack_count: float = 0.0
    fin_count: float = 0.0
    rst_count: float = 0.0
    psh_count: float = 0.0
    urg_count: float = 0.0
    iat_mean: float = 0.0
    iat_std: float = 0.0
    iat_min: float = 0.0
    iat_max: float = 0.0
    pkt_size_mean: float = 0.0
    pkt_size_std: float = 0.0
    pkt_size_min: float = 0.0
    pkt_size_max: float = 0.0
    payload_entropy: float = 0.0
    tls_present: float = 0.0
    tls_sni_len: float = 0.0
    http_present: float = 0.0
    http_req_count: float = 0.0
    label: float = LABEL_UNKNOWN

    def to_tuple(self) -> tuple:
        return astuple(self)

    @classmethod
    def from_sequence(cls, seq: Sequence[Any]) -> "ConnectionRecord":
        if len(seq) != N_FIELDS:
            raise ValueError(f"expected {N_FIELDS} fields, got {len(seq)}")
        return cls(*seq)

    def as_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in dc_fields(self)}


# Guard against the dataclass and the wire schema drifting apart.
assert tuple(f.name for f in dc_fields(ConnectionRecord)) == FIELDS, "ConnectionRecord/FIELDS mismatch"


def record_to_dict(rec: Sequence[Any]) -> dict[str, Any]:
    """Expand a wire tuple into a JSON-friendly dict (API / alert payloads)."""
    return dict(zip(FIELDS, rec))


def pack_batch(records: Iterable[Sequence[Any]]) -> bytes:
    """Serialise a batch of record tuples into one msgpack blob."""
    return msgpack.packb(list(records), use_bin_type=True)


def unpack_batch(blob: bytes) -> list[list]:
    """Inverse of :func:`pack_batch`.  Returns lists (msgpack has no tuples)."""
    return msgpack.unpackb(blob, raw=False, use_list=True, strict_map_key=False)


def pack_scores(rows: Iterable[Sequence[Any]]) -> bytes:
    """Serialise ``(flow_id, score, label, latency_s)`` rows for the bench path."""
    return msgpack.packb(list(rows), use_bin_type=True)


unpack_scores = unpack_batch
