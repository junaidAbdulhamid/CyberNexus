"""Capture-path tests.

They build a pcap with scapy and replay it, so the full capture path — parse,
flow assembly, batching, publish — is exercised without root, without libpcap
and without a network interface.
"""
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent

from cybernexus.bus import MemoryBus
from cybernexus.config import Settings
from cybernexus.records import record_to_dict, unpack_batch

scapy_all = pytest.importorskip("scapy.all")


@pytest.fixture
def sample_pcap(tmp_path):
    from scapy.all import IP, TCP, UDP, Raw, wrpcap

    pkts, t = [], 1_700_000_000.0
    for i in range(12):                      # 12 complete HTTPS sessions
        sport, dst = 40000 + i, f"10.0.0.{i % 4 + 2}"
        seq = [
            (IP(src="10.1.1.5", dst=dst) / TCP(sport=sport, dport=443, flags="S"), 0.001),
            (IP(src=dst, dst="10.1.1.5") / TCP(sport=443, dport=sport, flags="SA"), 0.001),
            (IP(src="10.1.1.5", dst=dst) / TCP(sport=sport, dport=443, flags="PA")
             / Raw(b"x" * 200), 0.002),
            (IP(src=dst, dst="10.1.1.5") / TCP(sport=443, dport=sport, flags="PA")
             / Raw(b"y" * 800), 0.003),
            (IP(src="10.1.1.5", dst=dst) / TCP(sport=sport, dport=443, flags="FA"), 0.001),
            (IP(src=dst, dst="10.1.1.5") / TCP(sport=443, dport=sport, flags="FA"), 0.001),
        ]
        for pkt, gap in seq:
            pkt.time = t
            t += gap
            pkts.append(pkt)
    for i in range(6):                       # 6 DNS lookups that never "close"
        pkt = IP(src="10.1.1.9", dst="10.0.0.53") / UDP(sport=50000 + i, dport=53) / Raw(b"q" * 30)
        pkt.time = t
        t += 0.01
        pkts.append(pkt)
    path = tmp_path / "sample.pcap"
    wrpcap(str(path), pkts)
    return path


def read_stream(bus, settings):
    recs = []
    for _mid, blob in reversed(bus.tail(settings.conn_stream, 1000)):
        recs.extend(unpack_batch(blob))
    return recs


def test_pcap_replay_produces_connection_records(sample_pcap, monkeypatch):
    from capture import pcap_capture

    bus = MemoryBus(maxlen=10_000)
    settings = Settings()
    settings.bus = "memory"
    settings.conn_stream = "t:capture:conns"
    monkeypatch.setattr(pcap_capture, "make_bus", lambda *a, **k: bus)
    monkeypatch.setattr(pcap_capture, "Settings", lambda: settings)

    total = pcap_capture.run(pcap_capture.parse_args(["--pcap", str(sample_pcap), "--bus", "memory"]))
    assert total == 18                       # 12 TCP sessions + 6 DNS flows

    recs = read_stream(bus, settings)
    assert len(recs) == 18
    https = [record_to_dict(r) for r in recs if r[4] == 443]
    dns = [record_to_dict(r) for r in recs if r[4] == 53]
    assert len(https) == 12 and len(dns) == 6

    first = https[0]
    assert first["orig_pkts"] == 3 and first["resp_pkts"] == 3
    assert first["syn_count"] == 2 and first["fin_count"] == 2
    assert first["duration"] > 0
    assert first["protocol"] == 6
    assert first["label"] == -1.0
    assert dns[0]["protocol"] == 17 and dns[0]["syn_count"] == 0


def test_captured_records_extract_and_score(sample_pcap, monkeypatch, model_dir):
    """The capture path must produce records the model can actually consume."""
    from capture import pcap_capture
    from cybernexus.features import extract_batch
    from model.infer import InferenceEngine

    bus = MemoryBus(maxlen=10_000)
    settings = Settings()
    settings.bus = "memory"
    settings.conn_stream = "t:capture:conns2"
    monkeypatch.setattr(pcap_capture, "make_bus", lambda *a, **k: bus)
    monkeypatch.setattr(pcap_capture, "Settings", lambda: settings)
    pcap_capture.run(pcap_capture.parse_args(["--pcap", str(sample_pcap), "--bus", "memory"]))

    X = extract_batch(read_stream(bus, settings))
    scores = InferenceEngine(model_dir=model_dir).score(X)
    assert len(scores) == 18
    assert np.isfinite(scores).all()
    assert ((scores >= 0) & (scores <= 1)).all()


def test_anonymisation_replaces_addresses(sample_pcap, monkeypatch):
    from capture import pcap_capture

    bus = MemoryBus(maxlen=10_000)
    settings = Settings()
    settings.bus = "memory"
    settings.conn_stream = "t:capture:anon"
    settings.anonymize_ips = True
    monkeypatch.setattr(pcap_capture, "make_bus", lambda *a, **k: bus)
    monkeypatch.setattr(pcap_capture, "Settings", lambda: settings)
    pcap_capture.run(pcap_capture.parse_args(["--pcap", str(sample_pcap), "--bus", "memory"]))

    recs = read_stream(bus, settings)
    assert recs
    assert all(r[1].startswith("100.64.") and r[2].startswith("100.64.") for r in recs)


def test_max_packets_stops_early(sample_pcap, monkeypatch):
    from capture import pcap_capture

    bus = MemoryBus(maxlen=10_000)
    settings = Settings()
    settings.bus = "memory"
    settings.conn_stream = "t:capture:limited"
    monkeypatch.setattr(pcap_capture, "make_bus", lambda *a, **k: bus)
    monkeypatch.setattr(pcap_capture, "Settings", lambda: settings)
    total = pcap_capture.run(
        pcap_capture.parse_args(["--pcap", str(sample_pcap), "--bus", "memory", "--max-packets", "12"])
    )
    assert 0 < total < 18


def test_raw_ip_pcap_parses_in_a_clean_interpreter(sample_pcap, tmp_path):
    """Regression: the capture module must load scapy's layers before opening
    the file.

    This runs in a subprocess on purpose.  In-process tests import
    ``scapy.all`` themselves, which registers the link-layer decoders as a side
    effect and hides the bug — the capture tool then silently produced zero
    records for every raw-IP pcap.
    """
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        from capture.pcap_capture import iter_pcap, scapy_to_meta
        parsed = sum(1 for p in iter_pcap({str(sample_pcap)!r}) if scapy_to_meta(p) is not None)
        print(parsed)
    """)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert int(out.stdout.strip()) == 78, "every packet in the capture must parse"


def test_emit_ts_is_wall_clock_not_capture_time(sample_pcap, monkeypatch):
    """Replayed records must be stamped with publication time.

    The pcap's timestamps are from 2023; if they were used as ``emit_ts`` the
    scorer would report end-to-end latencies of several years.
    """
    import time

    from capture import pcap_capture
    from cybernexus.records import FIELD_INDEX

    bus = MemoryBus(maxlen=10_000)
    settings = Settings()
    settings.bus = "memory"
    settings.conn_stream = "t:capture:ts"
    monkeypatch.setattr(pcap_capture, "make_bus", lambda *a, **k: bus)
    monkeypatch.setattr(pcap_capture, "Settings", lambda: settings)

    before = time.time()
    pcap_capture.run(pcap_capture.parse_args(["--pcap", str(sample_pcap), "--bus", "memory"]))
    after = time.time()

    recs = read_stream(bus, settings)
    assert recs
    for rec in recs:
        assert before <= rec[FIELD_INDEX["emit_ts"]] <= after
        # the flow's own start time still comes from the capture
        assert rec[FIELD_INDEX["start_ts"]] < before
