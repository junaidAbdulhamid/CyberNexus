"""Capture exporter: packets in, connection records on the bus.

This is the *correctness* prototype the brief asks for.  scapy is used because
it parses everything and is trivial to run against a saved pcap in CI; it is
explicitly not the production capture path — see ``capture/NOTES.md`` for the
AF_PACKET/eBPF/DPDK/Zeek options and the rates each reaches.

Three sources, one code path:

``--pcap FILE``   replay a capture file (used by the tests, needs no privileges)
``--iface IF``    live sniff (needs root / CAP_NET_RAW)
``--stdin``       read pcap from a pipe, e.g. ``tcpdump -w - | ... --stdin``

Privacy: payload bytes are parsed in memory for TLS/HTTP *metadata* only and are
never written to the bus.  ``--store-payloads`` opts in to publishing a
truncated payload sample on a separate stream, and prints a warning when it does.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Iterator

from capture.parse import anonymize_ip, is_http_request, tls_sni_length
from cybernexus.bus import make_bus
from cybernexus.config import Settings
from cybernexus.flowtable import FlowTable, PacketMeta
from cybernexus.logging_setup import setup_logging
from cybernexus.metrics import RECORDS, RateMeter
from cybernexus.records import FIELD_INDEX, pack_batch

EMIT_TS = FIELD_INDEX["emit_ts"]

log = logging.getLogger("capture")

MAX_INSPECT = 512  # bytes of payload parsed for TLS/HTTP metadata


def scapy_to_meta(pkt, anonymize: bool = False, salt: str = "") -> PacketMeta | None:
    """Convert one scapy packet into the flow table's input tuple."""
    from scapy.layers.inet import IP, TCP, UDP
    from scapy.layers.inet6 import IPv6

    if IP in pkt:
        ip = pkt[IP]
        src, dst, proto = ip.src, ip.dst, int(ip.proto)
    elif IPv6 in pkt:
        ip = pkt[IPv6]
        src, dst, proto = ip.src, ip.dst, int(ip.nh)
    else:
        return None

    sport = dport = 0
    flags = 0
    payload = b""
    if TCP in pkt:
        tcp = pkt[TCP]
        sport, dport, proto = int(tcp.sport), int(tcp.dport), 6
        flags = int(tcp.flags)
        payload = bytes(tcp.payload)[:MAX_INSPECT]
    elif UDP in pkt:
        udp = pkt[UDP]
        sport, dport, proto = int(udp.sport), int(udp.dport), 17
        payload = bytes(udp.payload)[:MAX_INSPECT]

    if anonymize:
        src, dst = anonymize_ip(src, salt), anonymize_ip(dst, salt)

    return PacketMeta(
        ts=float(pkt.time),
        src_ip=src, dst_ip=dst, src_port=sport, dst_port=dport, protocol=proto,
        length=len(pkt), tcp_flags=flags, payload_len=len(payload),
        tls_sni_len=tls_sni_length(payload) if payload else 0,
        is_http_request=is_http_request(payload) if payload else False,
    )


class CaptureExporter:
    """Owns the flow table and the publish batching."""

    def __init__(self, bus, settings: Settings, batch_size: int | None = None):
        self.bus = bus
        self.settings = settings
        self.batch_size = batch_size or settings.publish_batch
        self.table = FlowTable(
            idle_timeout=settings.flow_idle_timeout_s,
            active_timeout=settings.flow_active_timeout_s,
            max_flows=settings.max_flows,
        )
        self._pending: list[tuple] = []
        self._last_flush = time.monotonic()
        self._last_expire = 0.0
        self.meter = RateMeter()

    def feed(self, pkt: PacketMeta) -> None:
        self._pending.extend(self.table.add_packet(pkt))
        # Timeout sweeps are driven by packet timestamps so a pcap replay
        # behaves exactly like a live capture of the same traffic.
        if pkt.ts - self._last_expire >= 1.0:
            self._last_expire = pkt.ts
            self._pending.extend(self.table.expire(pkt.ts))
        if len(self._pending) >= self.batch_size:
            self.flush()
        elif time.monotonic() - self._last_flush >= self.settings.flush_interval_s:
            self.flush()

    def flush(self, final_ts: float | None = None) -> int:
        if final_ts is not None:
            self._pending.extend(self.table.flush(final_ts))
        if not self._pending:
            self._last_flush = time.monotonic()
            return 0
        n = len(self._pending)
        # `emit_ts` means "when this record entered the pipeline", and downstream
        # latency is measured against it.  The flow table can only know the
        # packet timestamp, which equals wall clock for a live capture but is
        # the capture-file time during a replay - so stamp it here, at the point
        # of publication, where the distinction is unambiguous.  `start_ts` keeps
        # the capture timeline.
        now = time.time()
        for start in range(0, n, self.batch_size):
            chunk = [
                r[:EMIT_TS] + (now,) + r[EMIT_TS + 1:]
                for r in self._pending[start:start + self.batch_size]
            ]
            self.bus.publish(self.settings.conn_stream, [pack_batch(chunk)])
        self._pending.clear()
        self._last_flush = time.monotonic()
        self.meter.add(n)
        RECORDS.labels("capture").inc(n)
        return n


def _load_scapy_layers() -> None:
    """Import the protocol layers *before* a capture file is opened.

    ``PcapReader`` resolves the link-layer decoder once, when the file header is
    read, from the registry that ``scapy.layers.inet`` populates on import.  If
    the layers are imported later — e.g. lazily inside the per-packet parser —
    the reader has already fallen back to ``Raw`` for the whole file and every
    packet silently fails to parse.  Raw-IP captures (DLT_RAW / DLT_IPV4, what
    ``tcpdump -i any`` and most synthetic pcaps produce) are the common victim.
    """
    # Imported for the registration side effect, not for the names.
    import scapy.layers.inet  # noqa: F401
    import scapy.layers.inet6  # noqa: F401
    import scapy.layers.l2  # noqa: F401


def iter_pcap(path: str) -> Iterator:
    _load_scapy_layers()
    from scapy.utils import PcapReader

    with PcapReader(path) as reader:
        yield from reader


def run(args) -> int:
    settings = Settings()
    if args.bus:
        settings.bus = args.bus
    setup_logging(settings.log_level)
    if settings.store_payloads:
        log.warning(
            "CN_STORE_PAYLOADS is enabled: truncated payload samples will be "
            "published to %s. This stores connection content - make sure that is "
            "intended and permitted.", settings.conn_stream + ":payload",
        )
    bus = make_bus(settings.bus, settings.redis_url, settings.stream_maxlen)
    exporter = CaptureExporter(bus, settings)
    anonymize, salt = settings.anonymize_ips, settings.anonymize_salt
    n_packets = 0
    t0 = time.monotonic()
    last_ts = time.time()

    try:
        if args.pcap or args.stdin:
            source = iter_pcap(args.pcap if args.pcap else "-")
            for pkt in source:
                meta = scapy_to_meta(pkt, anonymize, salt)
                if meta is None:
                    continue
                n_packets += 1
                last_ts = meta.ts
                exporter.feed(meta)
                if args.max_packets and n_packets >= args.max_packets:
                    break
        else:
            _load_scapy_layers()
            from scapy.sendrecv import AsyncSniffer

            log.info("sniffing %s (filter: %s)", args.iface or settings.iface, settings.bpf_filter)

            def handle(pkt):
                nonlocal n_packets, last_ts
                meta = scapy_to_meta(pkt, anonymize, salt)
                if meta is not None:
                    n_packets += 1
                    last_ts = meta.ts
                    exporter.feed(meta)

            sniffer = AsyncSniffer(
                iface=args.iface or settings.iface,
                filter=settings.bpf_filter or None,
                prn=handle,
                store=False,
            )
            sniffer.start()
            deadline = time.monotonic() + args.max_seconds if args.max_seconds else 0.0
            while sniffer.running:
                time.sleep(0.5)
                exporter.flush()
                if deadline and time.monotonic() > deadline:
                    break
            sniffer.stop()
    except KeyboardInterrupt:  # pragma: no cover
        log.info("interrupted")
    finally:
        exporter.flush(final_ts=last_ts + settings.flow_idle_timeout_s)
        bus.close()

    elapsed = max(time.monotonic() - t0, 1e-6)
    if n_packets == 0 and (args.pcap or args.stdin):
        log.error("no packets could be parsed from %s - is it a pcap file?",
                  args.pcap or "<stdin>")
    elif n_packets and exporter.meter.total == 0:
        log.warning("%d packets parsed but no connection records were produced; "
                    "all flows may still be open (idle timeout is %.0fs)",
                    n_packets, settings.flow_idle_timeout_s)
    log.info(
        "captured %d packets -> %d connection records in %.2fs (%.0f pkt/s, %.0f conn/s); %s",
        n_packets, exporter.meter.total, elapsed, n_packets / elapsed,
        exporter.meter.total / elapsed, exporter.table.stats(),
    )
    return exporter.meter.total


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="CyberNexus capture exporter (pcap prototype)")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--pcap", help="replay a pcap file instead of sniffing")
    src.add_argument("--iface", help="interface to sniff (needs root)")
    src.add_argument("--stdin", action="store_true", help="read pcap from stdin")
    p.add_argument("--bus", default=None, choices=["redis", "memory"])
    p.add_argument("--max-packets", type=int, default=0)
    p.add_argument("--max-seconds", type=float, default=0.0)
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(0 if run(parse_args()) >= 0 else 1)
