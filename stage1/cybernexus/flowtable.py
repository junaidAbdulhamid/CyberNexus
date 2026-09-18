"""Packet-to-flow aggregation.

The capture stage sees packets; everything downstream works on connections, so
something has to hold per-flow state and decide when a flow is finished.  That
is this module.  It is deliberately free of any capture-library import: it
consumes plain ``PacketMeta`` tuples, which makes it unit-testable without
scapy, libpcap or root privileges, and lets a future Rust/eBPF exporter feed the
identical logic.

Expiry rules (the usual NetFlow triple):

* natural end — TCP FIN from both sides, or a RST;
* idle timeout — no packet for ``idle_timeout`` seconds;
* active timeout — a long-lived flow is cut and re-opened every
  ``active_timeout`` seconds so that a multi-hour session still produces
  verdicts while it is happening rather than only when it ends.

Memory is bounded by ``max_flows``: past that, the oldest flows are force-
expired.  An unbounded flow table is how capture processes die during a scan.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterator, NamedTuple

from .records import FIELD_INDEX, LABEL_UNKNOWN, N_FIELDS


class PacketMeta(NamedTuple):
    """The only thing the flow table needs to know about a packet."""

    ts: float
    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int
    protocol: int
    length: int          # bytes on the wire
    tcp_flags: int       # raw TCP flag byte (0 for non-TCP)
    payload_len: int = 0
    tls_sni_len: int = 0
    is_http_request: bool = False


# TCP flag bits.
FIN, SYN, RST, PSH, ACK, URG = 0x01, 0x02, 0x04, 0x08, 0x10, 0x20

_SIZE_BIN = 64  # bytes per packet-size bucket for the entropy feature


def flow_key(pkt: PacketMeta) -> tuple:
    """Bidirectional 5-tuple key: both directions map to the same flow."""
    a = (pkt.src_ip, pkt.src_port)
    b = (pkt.dst_ip, pkt.dst_port)
    return (a, b, pkt.protocol) if a <= b else (b, a, pkt.protocol)


@dataclass(slots=True)
class FlowState:
    key: tuple
    flow_id: str
    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int
    protocol: int
    start_ts: float
    last_ts: float
    orig_bytes: float = 0.0
    resp_bytes: float = 0.0
    orig_pkts: float = 0.0
    resp_pkts: float = 0.0
    syn: float = 0.0
    ack: float = 0.0
    fin: float = 0.0
    rst: float = 0.0
    psh: float = 0.0
    urg: float = 0.0
    # Running moments, so packet-size and inter-arrival statistics never need
    # the per-packet history to be retained.
    iat_n: float = 0.0
    iat_sum: float = 0.0
    iat_sq: float = 0.0
    iat_min: float = math.inf
    iat_max: float = 0.0
    size_sum: float = 0.0
    size_sq: float = 0.0
    size_min: float = math.inf
    size_max: float = 0.0
    size_bins: dict = field(default_factory=dict)
    tls_sni_len: float = 0.0
    http_reqs: float = 0.0
    fin_seen_orig: bool = False
    fin_seen_resp: bool = False
    closed: bool = False

    def update(self, pkt: PacketMeta, forward: bool) -> None:
        gap = pkt.ts - self.last_ts
        if self.orig_pkts + self.resp_pkts > 0 and gap >= 0:
            self.iat_n += 1
            self.iat_sum += gap
            self.iat_sq += gap * gap
            if gap < self.iat_min:
                self.iat_min = gap
            if gap > self.iat_max:
                self.iat_max = gap
        self.last_ts = pkt.ts

        size = float(pkt.length)
        self.size_sum += size
        self.size_sq += size * size
        if size < self.size_min:
            self.size_min = size
        if size > self.size_max:
            self.size_max = size
        b = int(size) // _SIZE_BIN
        self.size_bins[b] = self.size_bins.get(b, 0) + 1

        if forward:
            self.orig_pkts += 1
            self.orig_bytes += size
        else:
            self.resp_pkts += 1
            self.resp_bytes += size

        flags = pkt.tcp_flags
        if flags:
            if flags & SYN:
                self.syn += 1
            if flags & ACK:
                self.ack += 1
            if flags & PSH:
                self.psh += 1
            if flags & URG:
                self.urg += 1
            if flags & RST:
                self.rst += 1
                self.closed = True
            if flags & FIN:
                self.fin += 1
                if forward:
                    self.fin_seen_orig = True
                else:
                    self.fin_seen_resp = True
                if self.fin_seen_orig and self.fin_seen_resp:
                    self.closed = True

        if pkt.tls_sni_len:
            self.tls_sni_len = max(self.tls_sni_len, float(pkt.tls_sni_len))
        if pkt.is_http_request:
            self.http_reqs += 1

    # -- export ----------------------------------------------------------
    def entropy(self) -> float:
        total = sum(self.size_bins.values())
        if total <= 0:
            return 0.0
        acc = 0.0
        for count in self.size_bins.values():
            p = count / total
            acc -= p * math.log2(p)
        return acc

    def to_record(self, now: float | None = None) -> tuple:
        n_pkts = self.orig_pkts + self.resp_pkts
        mean_size = self.size_sum / n_pkts if n_pkts else 0.0
        var_size = max(self.size_sq / n_pkts - mean_size * mean_size, 0.0) if n_pkts else 0.0
        iat_mean = self.iat_sum / self.iat_n if self.iat_n else 0.0
        iat_var = max(self.iat_sq / self.iat_n - iat_mean * iat_mean, 0.0) if self.iat_n else 0.0
        rec = [0.0] * N_FIELDS
        rec[FIELD_INDEX["flow_id"]] = self.flow_id
        rec[FIELD_INDEX["src_ip"]] = self.src_ip
        rec[FIELD_INDEX["dst_ip"]] = self.dst_ip
        rec[FIELD_INDEX["src_port"]] = float(self.src_port)
        rec[FIELD_INDEX["dst_port"]] = float(self.dst_port)
        rec[FIELD_INDEX["protocol"]] = float(self.protocol)
        rec[FIELD_INDEX["start_ts"]] = self.start_ts
        rec[FIELD_INDEX["emit_ts"]] = now if now is not None else self.last_ts
        rec[FIELD_INDEX["duration"]] = max(self.last_ts - self.start_ts, 0.0)
        rec[FIELD_INDEX["orig_bytes"]] = self.orig_bytes
        rec[FIELD_INDEX["resp_bytes"]] = self.resp_bytes
        rec[FIELD_INDEX["orig_pkts"]] = self.orig_pkts
        rec[FIELD_INDEX["resp_pkts"]] = self.resp_pkts
        rec[FIELD_INDEX["syn_count"]] = self.syn
        rec[FIELD_INDEX["ack_count"]] = self.ack
        rec[FIELD_INDEX["fin_count"]] = self.fin
        rec[FIELD_INDEX["rst_count"]] = self.rst
        rec[FIELD_INDEX["psh_count"]] = self.psh
        rec[FIELD_INDEX["urg_count"]] = self.urg
        rec[FIELD_INDEX["iat_mean"]] = iat_mean
        rec[FIELD_INDEX["iat_std"]] = math.sqrt(iat_var)
        rec[FIELD_INDEX["iat_min"]] = 0.0 if self.iat_min is math.inf else self.iat_min
        rec[FIELD_INDEX["iat_max"]] = self.iat_max
        rec[FIELD_INDEX["pkt_size_mean"]] = mean_size
        rec[FIELD_INDEX["pkt_size_std"]] = math.sqrt(var_size)
        rec[FIELD_INDEX["pkt_size_min"]] = 0.0 if self.size_min is math.inf else self.size_min
        rec[FIELD_INDEX["pkt_size_max"]] = self.size_max
        rec[FIELD_INDEX["payload_entropy"]] = self.entropy()
        rec[FIELD_INDEX["tls_present"]] = 1.0 if self.tls_sni_len else 0.0
        rec[FIELD_INDEX["tls_sni_len"]] = self.tls_sni_len
        rec[FIELD_INDEX["http_present"]] = 1.0 if self.http_reqs else 0.0
        rec[FIELD_INDEX["http_req_count"]] = self.http_reqs
        rec[FIELD_INDEX["label"]] = LABEL_UNKNOWN
        return tuple(rec)


class FlowTable:
    """Bounded, timeout-driven flow cache."""

    def __init__(
        self,
        idle_timeout: float = 15.0,
        active_timeout: float = 120.0,
        max_flows: int = 250_000,
        id_prefix: str = "c",
    ):
        self.idle_timeout = idle_timeout
        self.active_timeout = active_timeout
        self.max_flows = max_flows
        self.id_prefix = id_prefix
        self._flows: dict[tuple, FlowState] = {}
        self._counter = 0
        self.expired_closed = 0
        self.expired_idle = 0
        self.expired_active = 0
        self.evicted = 0

    def __len__(self) -> int:
        return len(self._flows)

    def add_packet(self, pkt: PacketMeta) -> list[tuple]:
        """Feed one packet.  Returns records for any flow that ended with it."""
        key = flow_key(pkt)
        state = self._flows.get(key)
        if state is None:
            self._counter += 1
            state = FlowState(
                key=key,
                flow_id=f"{self.id_prefix}{self._counter:x}",
                src_ip=pkt.src_ip, dst_ip=pkt.dst_ip,
                src_port=pkt.src_port, dst_port=pkt.dst_port,
                protocol=pkt.protocol, start_ts=pkt.ts, last_ts=pkt.ts,
            )
            self._flows[key] = state
            forward = True
        else:
            forward = (pkt.src_ip, pkt.src_port) == (state.src_ip, state.src_port)
        state.update(pkt, forward)

        out: list[tuple] = []
        if state.closed:
            out.append(state.to_record(pkt.ts))
            del self._flows[key]
            self.expired_closed += 1
        elif pkt.ts - state.start_ts >= self.active_timeout:
            out.append(state.to_record(pkt.ts))
            del self._flows[key]
            self.expired_active += 1
        if len(self._flows) > self.max_flows:
            out.extend(self._evict(pkt.ts))
        return out

    def _evict(self, now: float) -> list[tuple]:
        """Force out the least-recently-seen flows when over the cap."""
        overflow = len(self._flows) - self.max_flows
        victims = sorted(self._flows.values(), key=lambda s: s.last_ts)[:overflow]
        out = []
        for state in victims:
            out.append(state.to_record(now))
            self._flows.pop(state.key, None)
            self.evicted += 1
        return out

    def expire(self, now: float, force: bool = False) -> list[tuple]:
        """Flush flows past their idle/active timeout (call on a timer)."""
        out: list[tuple] = []
        for key, state in list(self._flows.items()):
            if force:
                pass
            elif now - state.last_ts < self.idle_timeout and now - state.start_ts < self.active_timeout:
                continue
            else:
                if now - state.last_ts >= self.idle_timeout:
                    self.expired_idle += 1
                else:
                    self.expired_active += 1
            out.append(state.to_record(now))
            del self._flows[key]
        return out

    def flush(self, now: float) -> list[tuple]:
        return self.expire(now, force=True)

    def stats(self) -> dict:
        return {
            "open_flows": len(self._flows),
            "expired_closed": self.expired_closed,
            "expired_idle": self.expired_idle,
            "expired_active": self.expired_active,
            "evicted": self.evicted,
        }


def merge_records(records: list[tuple]) -> Iterator[tuple]:
    """Merge partial records that share a ``flow_id``.

    A capture exporter that cuts long flows on the active timeout emits several
    records per connection.  Ingest folds them back together so the model sees
    one connection, exactly as it was trained.  Counters add; extrema take the
    outer bound; means are re-weighted by packet count.
    """
    merged: dict[str, list] = {}
    order: list[str] = []
    fi = FIELD_INDEX
    for rec in records:
        fid = rec[fi["flow_id"]]
        cur = merged.get(fid)
        if cur is None:
            merged[fid] = list(rec)
            order.append(fid)
            continue
        prev_pkts = cur[fi["orig_pkts"]] + cur[fi["resp_pkts"]]
        new_pkts = rec[fi["orig_pkts"]] + rec[fi["resp_pkts"]]
        total_pkts = prev_pkts + new_pkts
        for name in ("orig_bytes", "resp_bytes", "orig_pkts", "resp_pkts",
                     "syn_count", "ack_count", "fin_count", "rst_count",
                     "psh_count", "urg_count", "http_req_count"):
            cur[fi[name]] += rec[fi[name]]
        cur[fi["start_ts"]] = min(cur[fi["start_ts"]], rec[fi["start_ts"]])
        cur[fi["emit_ts"]] = max(cur[fi["emit_ts"]], rec[fi["emit_ts"]])
        cur[fi["duration"]] += rec[fi["duration"]]
        for name in ("iat_max", "pkt_size_max", "tls_sni_len", "payload_entropy"):
            cur[fi[name]] = max(cur[fi[name]], rec[fi[name]])
        for name in ("iat_min", "pkt_size_min"):
            cur[fi[name]] = min(cur[fi[name]], rec[fi[name]])
        if total_pkts > 0:
            for name in ("iat_mean", "iat_std", "pkt_size_mean", "pkt_size_std"):
                cur[fi[name]] = (
                    cur[fi[name]] * prev_pkts + rec[fi[name]] * new_pkts
                ) / total_pkts
        cur[fi["tls_present"]] = max(cur[fi["tls_present"]], rec[fi["tls_present"]])
        cur[fi["http_present"]] = max(cur[fi["http_present"]], rec[fi["http_present"]])
        if rec[fi["label"]] != LABEL_UNKNOWN:
            cur[fi["label"]] = rec[fi["label"]]
    for fid in order:
        yield tuple(merged[fid])
