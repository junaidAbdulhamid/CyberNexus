import pytest

from cybernexus.flowtable import (
    ACK, FIN, FlowTable, PacketMeta, PSH, RST, SYN, flow_key, merge_records,
)
from cybernexus.records import FIELD_INDEX, record_to_dict

T0 = 1_700_000_000.0


def pkt(ts_offset, src, dst, sport, dport, length=100, flags=0, proto=6, **kw):
    return PacketMeta(T0 + ts_offset, src, dst, sport, dport, proto, length, flags, **kw)


def test_flow_key_is_bidirectional():
    a = pkt(0, "10.0.0.1", "10.0.0.2", 1111, 80)
    b = pkt(0, "10.0.0.2", "10.0.0.1", 80, 1111)
    assert flow_key(a) == flow_key(b)


def test_flow_key_separates_protocols():
    a = pkt(0, "10.0.0.1", "10.0.0.2", 1111, 53, proto=6)
    b = pkt(0, "10.0.0.1", "10.0.0.2", 1111, 53, proto=17)
    assert flow_key(a) != flow_key(b)


def test_complete_tcp_session_emits_one_record():
    ft = FlowTable()
    out = []
    out += ft.add_packet(pkt(0.0, "10.0.0.1", "10.0.0.2", 5000, 443, 74, SYN))
    out += ft.add_packet(pkt(0.01, "10.0.0.2", "10.0.0.1", 443, 5000, 74, SYN | ACK))
    out += ft.add_packet(pkt(0.02, "10.0.0.1", "10.0.0.2", 5000, 443, 600, PSH | ACK))
    assert out == [] and len(ft) == 1
    out += ft.add_packet(pkt(0.03, "10.0.0.1", "10.0.0.2", 5000, 443, 60, FIN | ACK))
    out += ft.add_packet(pkt(0.04, "10.0.0.2", "10.0.0.1", 443, 5000, 60, FIN | ACK))
    assert len(out) == 1 and len(ft) == 0

    rec = record_to_dict(out[0])
    assert rec["src_ip"] == "10.0.0.1" and rec["dst_port"] == 443
    assert rec["orig_pkts"] == 3 and rec["resp_pkts"] == 2
    assert rec["orig_bytes"] == 74 + 600 + 60
    assert rec["resp_bytes"] == 74 + 60
    assert rec["duration"] == pytest.approx(0.04)
    assert rec["syn_count"] == 2 and rec["fin_count"] == 2
    assert rec["label"] == -1.0          # capture never claims ground truth
    assert rec["iat_mean"] == pytest.approx(0.01, rel=1e-6)


def test_rst_closes_the_flow():
    ft = FlowTable()
    ft.add_packet(pkt(0.0, "10.0.0.1", "10.0.0.2", 5000, 80, 74, SYN))
    out = ft.add_packet(pkt(0.01, "10.0.0.2", "10.0.0.1", 80, 5000, 60, RST))
    assert len(out) == 1 and ft.stats()["expired_closed"] == 1


def test_one_way_fin_does_not_close():
    ft = FlowTable()
    ft.add_packet(pkt(0.0, "10.0.0.1", "10.0.0.2", 5000, 80, 74, SYN))
    assert ft.add_packet(pkt(0.01, "10.0.0.1", "10.0.0.2", 5000, 80, 60, FIN | ACK)) == []
    assert len(ft) == 1


def test_idle_timeout_expires_flow():
    ft = FlowTable(idle_timeout=5.0)
    ft.add_packet(pkt(0.0, "10.0.0.1", "10.0.0.2", 5000, 53, 80, 0, proto=17))
    assert ft.expire(T0 + 1.0) == []
    out = ft.expire(T0 + 6.0)
    assert len(out) == 1 and ft.stats()["expired_idle"] == 1


def test_active_timeout_cuts_long_flow():
    ft = FlowTable(active_timeout=10.0)
    ft.add_packet(pkt(0.0, "10.0.0.1", "10.0.0.2", 5000, 22, 100, SYN))
    out = ft.add_packet(pkt(11.0, "10.0.0.1", "10.0.0.2", 5000, 22, 100, PSH))
    assert len(out) == 1
    assert ft.stats()["expired_active"] == 1
    assert record_to_dict(out[0])["duration"] == pytest.approx(11.0)


def test_max_flows_evicts_oldest():
    ft = FlowTable(max_flows=10)
    emitted = []
    for i in range(25):
        emitted += ft.add_packet(pkt(i * 0.001, "10.0.0.1", f"10.0.0.{i + 2}", 5000 + i, 80, 74, SYN))
    assert len(ft) <= 10
    assert ft.stats()["evicted"] == len(emitted) > 0


def test_flush_drains_everything():
    ft = FlowTable()
    for i in range(5):
        ft.add_packet(pkt(0.0, "10.0.0.1", f"10.0.0.{i + 2}", 5000 + i, 80, 74, SYN))
    out = ft.flush(T0 + 1.0)
    assert len(out) == 5 and len(ft) == 0


def test_entropy_reflects_size_variety():
    ft = FlowTable()
    for i in range(6):
        ft.add_packet(pkt(i * 0.01, "10.0.0.1", "10.0.0.2", 5000, 80, 100, PSH))
    uniform = record_to_dict(ft.flush(T0 + 100)[0])["payload_entropy"]

    ft2 = FlowTable()
    for i, size in enumerate((60, 300, 700, 1100, 1400, 1500)):
        ft2.add_packet(pkt(i * 0.01, "10.0.0.1", "10.0.0.2", 5001, 80, size, PSH))
    varied = record_to_dict(ft2.flush(T0 + 100)[0])["payload_entropy"]
    assert uniform == 0.0 and varied > 2.0


def test_tls_and_http_metadata_carry_through():
    ft = FlowTable()
    ft.add_packet(PacketMeta(T0, "10.0.0.1", "10.0.0.2", 5000, 443, 6, 500, PSH,
                             payload_len=400, tls_sni_len=19))
    ft.add_packet(PacketMeta(T0 + 0.1, "10.0.0.1", "10.0.0.2", 5000, 443, 6, 300, PSH,
                             payload_len=200, is_http_request=True))
    rec = record_to_dict(ft.flush(T0 + 60)[0])
    assert rec["tls_present"] == 1.0 and rec["tls_sni_len"] == 19
    assert rec["http_present"] == 1.0 and rec["http_req_count"] == 1


def test_stats_are_consistent():
    ft = FlowTable()
    assert ft.stats() == {"open_flows": 0, "expired_closed": 0, "expired_idle": 0,
                          "expired_active": 0, "evicted": 0}


# --- merge_records ------------------------------------------------------

def _partial(flow_id, **kw):
    from cybernexus.records import ConnectionRecord

    return ConnectionRecord(flow_id, "10.0.0.1", "10.0.0.2", **kw).to_tuple()


def test_merge_records_sums_counters():
    a = _partial("f1", duration=1.0, orig_bytes=100, orig_pkts=2, resp_pkts=0, syn_count=1)
    b = _partial("f1", duration=2.0, orig_bytes=400, orig_pkts=6, resp_pkts=2, ack_count=4)
    merged = list(merge_records([a, b]))
    assert len(merged) == 1
    rec = record_to_dict(merged[0])
    assert rec["orig_bytes"] == 500 and rec["orig_pkts"] == 8
    assert rec["duration"] == pytest.approx(3.0)
    assert rec["syn_count"] == 1 and rec["ack_count"] == 4


def test_merge_records_keeps_distinct_flows_and_order():
    recs = [_partial("a"), _partial("b"), _partial("a")]
    merged = list(merge_records(recs))
    assert [r[FIELD_INDEX["flow_id"]] for r in merged] == ["a", "b"]


def test_merge_records_preserves_known_label():
    a = _partial("f1", orig_pkts=1, label=-1.0)
    b = _partial("f1", orig_pkts=1, label=1.0)
    assert record_to_dict(list(merge_records([a, b]))[0])["label"] == 1.0


def test_merge_records_weights_means_by_packets():
    a = _partial("f1", orig_pkts=1, resp_pkts=0, pkt_size_mean=100.0)
    b = _partial("f1", orig_pkts=3, resp_pkts=0, pkt_size_mean=200.0)
    rec = record_to_dict(list(merge_records([a, b]))[0])
    assert rec["pkt_size_mean"] == pytest.approx((100 * 1 + 200 * 3) / 4)
