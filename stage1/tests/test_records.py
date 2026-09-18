import pytest

from cybernexus.records import (
    FIELDS, FIELD_INDEX, LABEL_UNKNOWN, NUMERIC_START, N_FIELDS, N_NUMERIC,
    ConnectionRecord, pack_batch, record_to_dict, unpack_batch,
)


def test_schema_matches_dataclass():
    rec = ConnectionRecord("f1", "10.0.0.1", "10.0.0.2")
    assert len(rec.to_tuple()) == N_FIELDS == len(FIELDS)
    assert N_NUMERIC == N_FIELDS - NUMERIC_START


def test_string_fields_come_first():
    # The whole fast path depends on the numeric tail being contiguous.
    rec = ConnectionRecord("f1", "10.0.0.1", "10.0.0.2").to_tuple()
    assert all(isinstance(v, str) for v in rec[:NUMERIC_START])
    assert all(isinstance(v, (int, float)) for v in rec[NUMERIC_START:])


def test_pack_roundtrip_preserves_values():
    recs = [
        ConnectionRecord(f"f{i}", "10.0.0.1", "10.0.0.2", src_port=1024 + i,
                         dst_port=443, orig_bytes=1234.5, label=1.0).to_tuple()
        for i in range(8)
    ]
    out = unpack_batch(pack_batch(recs))
    assert len(out) == 8
    assert [tuple(r) for r in out] == recs


def test_record_to_dict_keys():
    d = record_to_dict(ConnectionRecord("f", "a", "b").to_tuple())
    assert set(d) == set(FIELDS)
    assert d["label"] == LABEL_UNKNOWN


def test_from_sequence_validates_width():
    with pytest.raises(ValueError):
        ConnectionRecord.from_sequence(("only", "three", "things"))


def test_label_is_last_field():
    # unpack/feature code slices on this assumption.
    assert FIELDS[-1] == "label"
    assert FIELD_INDEX["label"] == N_FIELDS - 1
