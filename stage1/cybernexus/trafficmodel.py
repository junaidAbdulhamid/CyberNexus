"""Parametric traffic model used for training data *and* for bench traffic.

Why a simulator at all: CIC-IDS-2017 and UNSW-NB15 are the right datasets for
model quality (``model/dataset.py`` loads either), but neither ships as a live
stream and neither can be replayed at 10k connections/second without first being
turned into a generator.  This module is that generator.  It draws every field
of a connection record from per-class distributions calibrated to the shape of
those datasets — heavy-tailed byte counts, short-lived scan flows, metronomic
beacons — and it is fast enough to feed the bus at five-figure rates because
every class is sampled as whole numpy columns.

Honesty about what it proves: detection scores measured against this model are
scores *against this model*.  It is a stand-in for real traffic, not a
substitute.  Three design choices keep it from being a self-fulfilling
benchmark, and ``BENCHMARK.md`` reports all three:

1. benign traffic includes deliberate *confusers* — backup uploads that look
   like exfiltration, monitoring pollers that look like C2 beacons, IT
   vulnerability scans that look like port scans — so classes genuinely overlap;
2. training and benchmarking always use different seeds, and the bench harness
   can apply distribution ``drift`` to emulate a different environment;
3. ``model/train.py --dataset unsw|cic`` trains and evaluates on the real
   public datasets whenever they are present.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from .records import NUM_INDEX, N_NUMERIC

BENIGN = 0

#: Attack taxonomy.  ``label`` in a record is 0 for benign, 1 for attack; the
#: finer class id is carried alongside by the generators for per-class reporting.
CLASS_NAMES: tuple[str, ...] = (
    "benign_web_https",
    "benign_web_http",
    "benign_dns",
    "benign_ssh_interactive",
    "benign_smtp",
    "benign_smb",
    "benign_backup_upload",      # confuser for exfiltration
    "benign_monitoring_poll",    # confuser for C2 beaconing
    "benign_it_scan",            # confuser for port scanning
    "benign_failed_conn",        # confuser for scans and floods
    "attack_port_scan",
    "attack_syn_flood",
    "attack_ddos_http",
    "attack_brute_force",
    "attack_exfiltration",
    "attack_c2_beacon",
    "attack_web_injection",
)
CLASS_ID = {name: i for i, name in enumerate(CLASS_NAMES)}
ATTACK_CLASSES = tuple(n for n in CLASS_NAMES if n.startswith("attack_"))
BENIGN_CLASSES = tuple(n for n in CLASS_NAMES if n.startswith("benign_"))
IS_ATTACK = np.array([1.0 if n.startswith("attack_") else 0.0 for n in CLASS_NAMES])

#: Default class mix.  Benign dominates, as in any real network.
DEFAULT_MIX: dict[str, float] = {
    "benign_web_https": 0.33,
    "benign_web_http": 0.10,
    "benign_dns": 0.17,
    "benign_ssh_interactive": 0.04,
    "benign_smtp": 0.03,
    "benign_smb": 0.05,
    "benign_backup_upload": 0.015,
    "benign_monitoring_poll": 0.035,
    "benign_it_scan": 0.02,
    "benign_failed_conn": 0.04,
    "attack_port_scan": 0.035,
    "attack_syn_flood": 0.025,
    "attack_ddos_http": 0.025,
    "attack_brute_force": 0.03,
    "attack_exfiltration": 0.012,
    "attack_c2_beacon": 0.023,
    "attack_web_injection": 0.02,
}
ATTACK_FRACTION = sum(v for k, v in DEFAULT_MIX.items() if k.startswith("attack_"))

#: Default share of each batch pulled toward the opposite label (see the
#: "Ambiguity" section below).  0.05 puts the ceiling on detection at roughly
#: 97% for a 1% false-positive budget; ``bench/difficulty_sweep.py`` measures
#: how detection degrades as this is raised.
DEFAULT_HARDNESS = 0.05


def _lognorm(rng: np.random.Generator, n: int, median: float, sigma: float) -> np.ndarray:
    """Lognormal with an explicit median — the natural parameterisation for
    byte counts and durations, both of which are heavy-tailed in real traffic."""
    return np.exp(np.log(max(median, 1e-9)) + sigma * rng.standard_normal(n))


def _choice(rng: np.random.Generator, n: int, values, p=None) -> np.ndarray:
    return rng.choice(np.asarray(values, dtype=np.float64), size=n, p=p)


@dataclass(slots=True)
class _Cols:
    """Thin accessor so class samplers read like the record schema."""

    mat: np.ndarray

    def __setitem__(self, name: str, value) -> None:
        self.mat[:, NUM_INDEX[name]] = value

    def __getitem__(self, name: str) -> np.ndarray:
        return self.mat[:, NUM_INDEX[name]]


def _assemble(
    rng: np.random.Generator,
    n: int,
    *,
    protocol,
    dst_port,
    src_port=None,
    duration,
    orig_bytes,
    resp_bytes,
    orig_pkts,
    resp_pkts,
    syn=None,
    ack=None,
    fin=0.0,
    rst=0.0,
    psh=None,
    urg=0.0,
    iat_cv,
    size_cv,
    entropy,
    tls=0.0,
    sni_len=0.0,
    http=0.0,
    http_reqs=0.0,
) -> np.ndarray:
    """Build the numeric block for ``n`` flows of one class.

    Packet-size and inter-arrival statistics are *derived* from the byte and
    packet counts rather than drawn independently, so a generated flow is
    internally consistent: a model cannot learn a shortcut that would not exist
    in traffic parsed off the wire.
    """
    mat = np.zeros((n, N_NUMERIC), dtype=np.float64)
    c = _Cols(mat)

    orig_pkts = np.maximum(np.round(np.broadcast_to(orig_pkts, (n,))), 1.0)
    resp_pkts = np.maximum(np.round(np.broadcast_to(resp_pkts, (n,))), 0.0)
    orig_bytes = np.maximum(np.round(np.broadcast_to(orig_bytes, (n,))), 40.0 * orig_pkts)
    resp_bytes = np.maximum(np.round(np.broadcast_to(resp_bytes, (n,))), 40.0 * resp_pkts)
    duration = np.maximum(np.broadcast_to(duration, (n,)), 0.0)

    c["protocol"] = protocol
    c["dst_port"] = dst_port
    c["src_port"] = (
        rng.integers(49152, 65535, n).astype(np.float64) if src_port is None else src_port
    )
    c["duration"] = duration
    c["orig_bytes"] = orig_bytes
    c["resp_bytes"] = resp_bytes
    c["orig_pkts"] = orig_pkts
    c["resp_pkts"] = resp_pkts

    total_pkts = orig_pkts + resp_pkts
    total_bytes = orig_bytes + resp_bytes
    is_tcp = np.asarray(np.broadcast_to(protocol, (n,))) == 6

    syn = np.ones(n) if syn is None else syn
    ack = np.maximum(total_pkts - 2.0, 0.0) if ack is None else ack
    psh = np.maximum(total_pkts * 0.3, 0.0) if psh is None else psh
    for name, value in (
        ("syn_count", syn), ("ack_count", ack), ("fin_count", fin),
        ("rst_count", rst), ("psh_count", psh), ("urg_count", urg),
    ):
        # UDP/ICMP flows carry no TCP flags.
        c[name] = np.where(is_tcp, value, 0.0)

    # --- derived timing -------------------------------------------------
    gaps = np.maximum(total_pkts - 1.0, 1.0)
    iat_mean = duration / gaps
    iat_cv = np.maximum(np.broadcast_to(iat_cv, (n,)), 0.0)
    iat_std = iat_mean * iat_cv
    c["iat_mean"] = iat_mean
    c["iat_std"] = iat_std
    c["iat_min"] = np.maximum(iat_mean - 2.0 * iat_std, 0.0)
    c["iat_max"] = iat_mean + rng.uniform(1.5, 4.0, n) * iat_std

    # --- derived packet sizes -------------------------------------------
    mean_size = np.clip(total_bytes / np.maximum(total_pkts, 1.0), 40.0, 1500.0)
    size_cv = np.maximum(np.broadcast_to(size_cv, (n,)), 0.0)
    size_std = mean_size * size_cv
    c["pkt_size_mean"] = mean_size
    c["pkt_size_std"] = size_std
    c["pkt_size_min"] = np.clip(mean_size - 1.8 * size_std, 40.0, 1500.0)
    c["pkt_size_max"] = np.clip(mean_size + 2.2 * size_std, 40.0, 1500.0)

    # Entropy of the packet-size distribution: rises with both the spread of
    # sizes and the number of packets, and saturates at log2(#size-bins).
    ent = np.broadcast_to(entropy, (n,)) * np.log2(np.minimum(total_pkts, 64.0) + 1.0) / 6.0
    c["payload_entropy"] = np.clip(ent + rng.normal(0, 0.15, n), 0.0, 4.5)

    c["tls_present"] = tls
    c["tls_sni_len"] = sni_len
    c["http_present"] = http
    c["http_req_count"] = http_reqs
    return mat


# ---------------------------------------------------------------------------
# Per-class samplers.  Each returns the (n, N_NUMERIC) numeric block.
# ---------------------------------------------------------------------------

def _benign_web_https(rng, n):
    dur = _lognorm(rng, n, 1.8, 1.1)
    resp = _lognorm(rng, n, 14_000, 1.6)
    orig = _lognorm(rng, n, 900, 1.0)
    return _assemble(
        rng, n, protocol=6.0, dst_port=443.0, duration=dur,
        orig_bytes=orig, resp_bytes=resp,
        orig_pkts=np.maximum(orig / 420.0, 4.0), resp_pkts=np.maximum(resp / 1100.0, 4.0),
        fin=rng.integers(0, 3, n), iat_cv=rng.uniform(0.8, 2.6, n),
        size_cv=rng.uniform(0.35, 0.75, n), entropy=rng.uniform(2.2, 3.4, n),
        tls=1.0, sni_len=rng.integers(8, 48, n),
    )


def _benign_web_http(rng, n):
    dur = _lognorm(rng, n, 0.9, 1.0)
    resp = _lognorm(rng, n, 9_000, 1.5)
    orig = _lognorm(rng, n, 700, 0.8)
    return _assemble(
        rng, n, protocol=6.0, dst_port=_choice(rng, n, [80, 8080], [0.85, 0.15]), duration=dur,
        orig_bytes=orig, resp_bytes=resp,
        orig_pkts=np.maximum(orig / 400.0, 3.0), resp_pkts=np.maximum(resp / 1100.0, 3.0),
        fin=rng.integers(0, 3, n), iat_cv=rng.uniform(0.7, 2.4, n),
        size_cv=rng.uniform(0.35, 0.8, n), entropy=rng.uniform(2.0, 3.2, n),
        http=1.0, http_reqs=rng.integers(1, 8, n),
    )


def _benign_dns(rng, n):
    return _assemble(
        rng, n, protocol=17.0, dst_port=53.0, duration=_lognorm(rng, n, 0.03, 0.9),
        orig_bytes=rng.integers(60, 140, n), resp_bytes=rng.integers(80, 600, n),
        orig_pkts=1.0, resp_pkts=_choice(rng, n, [1, 2], [0.9, 0.1]),
        iat_cv=rng.uniform(0.2, 1.0, n), size_cv=rng.uniform(0.1, 0.4, n),
        entropy=rng.uniform(0.6, 1.6, n),
    )


def _benign_ssh_interactive(rng, n):
    dur = _lognorm(rng, n, 220.0, 1.2)
    pkts = np.maximum(dur * rng.uniform(0.5, 4.0, n), 20.0)
    return _assemble(
        rng, n, protocol=6.0, dst_port=22.0, duration=dur,
        orig_bytes=pkts * rng.uniform(60, 120, n), resp_bytes=pkts * rng.uniform(90, 400, n),
        orig_pkts=pkts * 0.5, resp_pkts=pkts * 0.5,
        psh=pkts * 0.8, fin=rng.integers(0, 3, n),
        iat_cv=rng.uniform(1.5, 4.5, n),  # human typing: very bursty
        size_cv=rng.uniform(0.5, 1.1, n), entropy=rng.uniform(2.4, 3.6, n),
    )


def _benign_smtp(rng, n):
    dur = _lognorm(rng, n, 3.0, 0.9)
    orig = _lognorm(rng, n, 25_000, 1.7)
    return _assemble(
        rng, n, protocol=6.0, dst_port=_choice(rng, n, [25, 587], [0.5, 0.5]), duration=dur,
        orig_bytes=orig, resp_bytes=_lognorm(rng, n, 1_200, 0.9),
        orig_pkts=np.maximum(orig / 1300.0, 6.0), resp_pkts=rng.integers(6, 30, n),
        fin=rng.integers(0, 3, n), iat_cv=rng.uniform(0.9, 2.4, n),
        size_cv=rng.uniform(0.3, 0.7, n), entropy=rng.uniform(2.0, 3.2, n),
    )


def _benign_smb(rng, n):
    dur = _lognorm(rng, n, 12.0, 1.3)
    total = _lognorm(rng, n, 180_000, 1.8)
    return _assemble(
        rng, n, protocol=6.0, dst_port=445.0, duration=dur,
        orig_bytes=total * 0.4, resp_bytes=total * 0.6,
        orig_pkts=np.maximum(total * 0.4 / 900.0, 8.0),
        resp_pkts=np.maximum(total * 0.6 / 1300.0, 8.0),
        fin=rng.integers(0, 3, n), iat_cv=rng.uniform(1.0, 3.0, n),
        size_cv=rng.uniform(0.4, 0.9, n), entropy=rng.uniform(2.4, 3.6, n),
    )


def _benign_backup_upload(rng, n):
    """Nightly backup: a large, long, outbound-heavy transfer.  Structurally
    this is what exfiltration looks like — the separation has to come from
    destination port, packet-size regularity and entropy, not volume alone."""
    dur = _lognorm(rng, n, 420.0, 0.9)
    orig = _lognorm(rng, n, 9.0e7, 1.1)
    return _assemble(
        rng, n, protocol=6.0, dst_port=_choice(rng, n, [22, 443, 873, 3260], [0.35, 0.3, 0.2, 0.15]),
        duration=dur, orig_bytes=orig, resp_bytes=_lognorm(rng, n, 40_000, 1.2),
        orig_pkts=orig / 1450.0, resp_pkts=np.maximum(orig / 1450.0 * 0.3, 10.0),
        fin=rng.integers(0, 3, n), iat_cv=rng.uniform(0.25, 0.9, n),
        size_cv=rng.uniform(0.05, 0.25, n),  # MTU-filling, very regular
        entropy=rng.uniform(1.0, 2.2, n), tls=_choice(rng, n, [0, 1], [0.5, 0.5]),
        sni_len=rng.integers(0, 30, n),
    )


def _benign_monitoring_poll(rng, n):
    """Prometheus/Nagios-style pollers: metronomic, like a C2 beacon, but aimed
    at internal service ports with a substantial response body."""
    period = _choice(rng, n, [5, 10, 15, 30, 60])
    pkts = rng.integers(8, 40, n).astype(np.float64)
    return _assemble(
        rng, n, protocol=6.0,
        dst_port=_choice(rng, n, [80, 443, 8080, 9090, 9100, 161], [0.2, 0.2, 0.15, 0.2, 0.15, 0.1]),
        duration=period * rng.uniform(0.02, 0.2, n),
        orig_bytes=pkts * rng.uniform(80, 220, n) * 0.4,
        resp_bytes=pkts * rng.uniform(600, 1400, n) * 0.6,
        orig_pkts=pkts * 0.4, resp_pkts=pkts * 0.6,
        fin=rng.integers(0, 3, n),
        iat_cv=rng.uniform(0.05, 0.45, n),  # regular, but not as flat as C2
        size_cv=rng.uniform(0.2, 0.6, n), entropy=rng.uniform(1.6, 2.8, n),
        http=1.0, http_reqs=rng.integers(1, 4, n),
    )


def _benign_it_scan(rng, n):
    """Authorised asset-discovery sweep from the IT subnet: same shape as a
    hostile port scan, but it completes handshakes against live services."""
    return _assemble(
        rng, n, protocol=6.0,
        dst_port=_choice(rng, n, [22, 80, 135, 443, 445, 3389, 5985]),
        duration=_lognorm(rng, n, 0.12, 1.0),
        orig_bytes=rng.integers(120, 600, n), resp_bytes=rng.integers(100, 2500, n),
        orig_pkts=rng.integers(3, 8, n), resp_pkts=rng.integers(2, 7, n),
        syn=1.0, fin=rng.integers(0, 2, n), rst=_choice(rng, n, [0, 1], [0.7, 0.3]),
        iat_cv=rng.uniform(0.3, 1.2, n), size_cv=rng.uniform(0.2, 0.6, n),
        entropy=rng.uniform(1.2, 2.4, n),
    )


def _benign_failed_conn(rng, n):
    """Ordinary broken connectivity: a stale bookmark, a service that moved, a
    client behind a dropped VPN.  Every real network produces a steady stream of
    these, and they are the single largest source of scan false positives."""
    resp_pkts = _choice(rng, n, [0, 1, 2], [0.4, 0.4, 0.2])
    return _assemble(
        rng, n, protocol=_choice(rng, n, [6, 17], [0.9, 0.1]),
        dst_port=_choice(rng, n, [80, 443, 22, 445, 3389, 8080, 5432, 3306, 9000]),
        duration=_lognorm(rng, n, 0.9, 1.8),  # SYN retries stretch these out
        orig_bytes=rng.integers(40, 260, n), resp_bytes=resp_pkts * rng.integers(40, 80, n),
        orig_pkts=_choice(rng, n, [1, 2, 3], [0.45, 0.35, 0.2]), resp_pkts=resp_pkts,
        syn=_choice(rng, n, [1, 2, 3], [0.5, 0.3, 0.2]), ack=0.0, psh=0.0,
        rst=_choice(rng, n, [0, 1], [0.45, 0.55]),
        iat_cv=rng.uniform(0.1, 1.2, n), size_cv=rng.uniform(0.0, 0.25, n),
        entropy=rng.uniform(0.0, 1.0, n),
    )


def _attack_port_scan(rng, n):
    """SYN scan: a handshake that never completes.  One or two packets out,
    at most a RST back, across a wide spread of destination ports."""
    resp_pkts = _choice(rng, n, [0, 1], [0.55, 0.45])
    return _assemble(
        rng, n, protocol=6.0, dst_port=rng.integers(1, 65535, n).astype(np.float64),
        duration=_lognorm(rng, n, 0.004, 1.3),
        orig_bytes=rng.integers(40, 80, n), resp_bytes=resp_pkts * rng.integers(40, 60, n),
        orig_pkts=_choice(rng, n, [1, 2], [0.8, 0.2]), resp_pkts=resp_pkts,
        syn=1.0, ack=0.0, rst=resp_pkts, psh=0.0,
        iat_cv=rng.uniform(0.1, 0.8, n), size_cv=rng.uniform(0.0, 0.15, n),
        entropy=rng.uniform(0.0, 0.7, n),
    )


def _attack_syn_flood(rng, n):
    """Volumetric SYN flood: bursts of SYNs, no response, spoofed sources."""
    pkts = rng.integers(2, 40, n).astype(np.float64)
    return _assemble(
        rng, n, protocol=6.0, dst_port=_choice(rng, n, [80, 443, 22, 53, 3389]),
        duration=_lognorm(rng, n, 0.02, 1.0),
        orig_bytes=pkts * rng.integers(40, 64, n), resp_bytes=0.0,
        orig_pkts=pkts, resp_pkts=0.0,
        syn=pkts, ack=0.0, psh=0.0,
        iat_cv=rng.uniform(0.05, 0.5, n), size_cv=rng.uniform(0.0, 0.1, n),
        entropy=rng.uniform(0.0, 0.5, n),
        src_port=rng.integers(1024, 65535, n).astype(np.float64),
    )


def _attack_ddos_http(rng, n):
    """Layer-7 flood: real requests, minimal responses, no session depth."""
    pkts = rng.integers(4, 14, n).astype(np.float64)
    return _assemble(
        rng, n, protocol=6.0, dst_port=_choice(rng, n, [80, 443, 8080], [0.5, 0.35, 0.15]),
        duration=_lognorm(rng, n, 0.25, 0.8),
        orig_bytes=pkts * rng.uniform(120, 260, n) * 0.6,
        resp_bytes=_lognorm(rng, n, 500, 1.0),
        orig_pkts=pkts * 0.6, resp_pkts=np.maximum(pkts * 0.4, 1.0),
        syn=1.0, rst=_choice(rng, n, [0, 1], [0.6, 0.4]),
        iat_cv=rng.uniform(0.1, 0.7, n), size_cv=rng.uniform(0.05, 0.3, n),
        entropy=rng.uniform(0.6, 1.8, n), http=1.0, http_reqs=_choice(rng, n, [1, 2], [0.85, 0.15]),
    )


def _attack_brute_force(rng, n):
    """Credential stuffing against ssh/rdp/smb: short, near-identical flows."""
    pkts = rng.integers(10, 30, n).astype(np.float64)
    return _assemble(
        rng, n, protocol=6.0,
        dst_port=_choice(rng, n, [22, 3389, 445, 21, 1433], [0.4, 0.25, 0.15, 0.1, 0.1]),
        duration=_lognorm(rng, n, 0.9, 0.5),
        orig_bytes=pkts * rng.uniform(90, 160, n) * 0.5,
        resp_bytes=pkts * rng.uniform(90, 200, n) * 0.5,
        orig_pkts=pkts * 0.5, resp_pkts=pkts * 0.5,
        syn=1.0, fin=_choice(rng, n, [0, 1], [0.4, 0.6]), rst=_choice(rng, n, [0, 1], [0.5, 0.5]),
        psh=pkts * 0.5,
        iat_cv=rng.uniform(0.08, 0.5, n),   # scripted, not human
        size_cv=rng.uniform(0.05, 0.3, n),  # same payloads every attempt
        entropy=rng.uniform(1.0, 2.0, n),
    )


def _attack_exfiltration(rng, n):
    """Staged outbound transfer: large, long, one-directional, high entropy,
    usually to a non-standard port."""
    dur = _lognorm(rng, n, 150.0, 1.2)
    orig = _lognorm(rng, n, 4.0e7, 1.4)
    return _assemble(
        rng, n, protocol=6.0,
        dst_port=np.where(
            rng.random(n) < 0.45,
            _choice(rng, n, [443, 80, 53]),
            rng.integers(1024, 65535, n).astype(np.float64),
        ),
        duration=dur, orig_bytes=orig, resp_bytes=_lognorm(rng, n, 2_500, 1.2),
        orig_pkts=orig / rng.uniform(700, 1200, n),
        resp_pkts=rng.integers(4, 40, n),
        fin=rng.integers(0, 2, n), psh=orig / 900.0,
        iat_cv=rng.uniform(0.6, 2.2, n),    # throttled / chunked
        size_cv=rng.uniform(0.35, 0.8, n),  # padded chunks, unlike a clean backup
        entropy=rng.uniform(3.0, 4.2, n),   # compressed+encrypted archive
        tls=_choice(rng, n, [0, 1], [0.6, 0.4]), sni_len=rng.integers(0, 12, n),
    )


def _attack_c2_beacon(rng, n):
    """Implant check-in: small, symmetric, and metronomic (iat_cv ~ 0)."""
    pkts = rng.integers(6, 24, n).astype(np.float64)
    return _assemble(
        rng, n, protocol=_choice(rng, n, [6, 17], [0.85, 0.15]),
        dst_port=np.where(
            rng.random(n) < 0.5,
            _choice(rng, n, [443, 8443, 53, 8080]),
            rng.integers(1024, 65535, n).astype(np.float64),
        ),
        duration=_lognorm(rng, n, 1.2, 0.8),
        orig_bytes=pkts * rng.uniform(110, 260, n) * 0.5,
        resp_bytes=pkts * rng.uniform(110, 300, n) * 0.5,
        orig_pkts=pkts * 0.5, resp_pkts=pkts * 0.5,
        syn=1.0, fin=_choice(rng, n, [0, 1], [0.3, 0.7]), psh=pkts * 0.6,
        iat_cv=rng.uniform(0.01, 0.22, n),
        size_cv=rng.uniform(0.02, 0.22, n),
        entropy=rng.uniform(2.6, 3.8, n),
        tls=_choice(rng, n, [0, 1], [0.45, 0.55]), sni_len=rng.integers(0, 14, n),
    )


def _attack_web_injection(rng, n):
    """SQLi / XSS / traversal probes: oversized requests, error-sized replies."""
    reqs = rng.integers(1, 12, n).astype(np.float64)
    orig = reqs * rng.uniform(800, 4000, n)
    return _assemble(
        rng, n, protocol=6.0, dst_port=_choice(rng, n, [80, 443, 8080], [0.55, 0.3, 0.15]),
        duration=_lognorm(rng, n, 0.7, 0.9),
        orig_bytes=orig, resp_bytes=reqs * rng.uniform(200, 2200, n),
        orig_pkts=np.maximum(orig / 900.0, reqs * 2.0), resp_pkts=np.maximum(reqs * 2.0, 2.0),
        syn=1.0, fin=_choice(rng, n, [0, 1], [0.4, 0.6]), psh=reqs * 2.0,
        iat_cv=rng.uniform(0.1, 0.9, n), size_cv=rng.uniform(0.3, 0.9, n),
        entropy=rng.uniform(1.8, 3.2, n), http=1.0, http_reqs=reqs,
        tls=_choice(rng, n, [0, 1], [0.7, 0.3]), sni_len=rng.integers(0, 24, n),
    )


SAMPLERS: dict[str, Callable[[np.random.Generator, int], np.ndarray]] = {
    "benign_web_https": _benign_web_https,
    "benign_web_http": _benign_web_http,
    "benign_dns": _benign_dns,
    "benign_ssh_interactive": _benign_ssh_interactive,
    "benign_smtp": _benign_smtp,
    "benign_smb": _benign_smb,
    "benign_backup_upload": _benign_backup_upload,
    "benign_monitoring_poll": _benign_monitoring_poll,
    "benign_it_scan": _benign_it_scan,
    "benign_failed_conn": _benign_failed_conn,
    "attack_port_scan": _attack_port_scan,
    "attack_syn_flood": _attack_syn_flood,
    "attack_ddos_http": _attack_ddos_http,
    "attack_brute_force": _attack_brute_force,
    "attack_exfiltration": _attack_exfiltration,
    "attack_c2_beacon": _attack_c2_beacon,
    "attack_web_injection": _attack_web_injection,
}
assert set(SAMPLERS) == set(CLASS_NAMES)


# ---------------------------------------------------------------------------
# Ambiguity: the part that keeps the benchmark honest
# ---------------------------------------------------------------------------
#
# Sampled straight from the class distributions above, the two labels separate
# almost perfectly and any classifier scores ~100% — a number that says more
# about the simulator than about the detector.  Real traffic does not work that
# way: a slow scan looks like a handful of failed connections, a jittered
# implant looks like a monitoring poller, a staged upload looks like a backup
# job.  Those cases are where detection is actually decided.
#
# So a configurable fraction of every batch is *blended*: the row is pulled part
# of the way toward a random row of the opposite label while keeping its own
# label.  The blend weight is drawn per row, which produces a continuum from
# "obvious" to "genuinely indistinguishable" rather than a second, separable
# cluster — the irreducible error a real detector faces.  Volume-like fields
# blend geometrically (they are lognormal), entropy blends linearly, and
# categorical fields (protocol, ports, TLS/HTTP markers) are taken wholesale
# from one side or the other, since an average of port 22 and port 443 is
# meaningless.  Derived invariants are recomputed afterwards so a blended row
# stays internally consistent and cannot be spotted as an artefact.

_LOG_BLEND = (
    "duration", "orig_bytes", "resp_bytes", "orig_pkts", "resp_pkts",
    "syn_count", "ack_count", "fin_count", "rst_count", "psh_count", "urg_count",
    "iat_std", "pkt_size_std",
)
_LINEAR_BLEND = ("payload_entropy",)
_CATEGORICAL = (
    "protocol", "src_port", "dst_port", "tls_present", "tls_sni_len",
    "http_present", "http_req_count",
)


def _blend_rows(rng: np.random.Generator, mat: np.ndarray, rows: np.ndarray,
                partners: np.ndarray, weights: np.ndarray) -> None:
    """Move ``rows`` ``weights`` of the way toward ``partners`` in place."""
    if rows.size == 0:
        return
    w = weights[:, None]
    for name in _LOG_BLEND:
        i = NUM_INDEX[name]
        a = np.maximum(mat[rows, i], 1e-6)
        b = np.maximum(mat[partners, i], 1e-6)
        mat[rows, i] = np.exp((1 - weights) * np.log(a) + weights * np.log(b))
    for name in _LINEAR_BLEND:
        i = NUM_INDEX[name]
        mat[rows, i] = (1 - weights) * mat[rows, i] + weights * mat[partners, i]
    take_partner = rng.random(rows.size) < weights
    for name in _CATEGORICAL:
        i = NUM_INDEX[name]
        mat[rows, i] = np.where(take_partner, mat[partners, i], mat[rows, i])
    del w

    # --- restore the invariants the samplers guarantee --------------------
    sub = mat[rows]
    orig_p = np.maximum(np.round(sub[:, NUM_INDEX["orig_pkts"]]), 1.0)
    resp_p = np.maximum(np.round(sub[:, NUM_INDEX["resp_pkts"]]), 0.0)
    total_p = orig_p + resp_p
    orig_b = np.maximum(sub[:, NUM_INDEX["orig_bytes"]], 40.0 * orig_p)
    resp_b = np.maximum(sub[:, NUM_INDEX["resp_bytes"]], 40.0 * resp_p)
    sub[:, NUM_INDEX["orig_pkts"]] = orig_p
    sub[:, NUM_INDEX["resp_pkts"]] = resp_p
    sub[:, NUM_INDEX["orig_bytes"]] = orig_b
    sub[:, NUM_INDEX["resp_bytes"]] = resp_b
    is_tcp = sub[:, NUM_INDEX["protocol"]] == 6
    for name in ("syn_count", "ack_count", "fin_count", "rst_count", "psh_count", "urg_count"):
        sub[:, NUM_INDEX[name]] = np.where(is_tcp, np.minimum(sub[:, NUM_INDEX[name]], total_p), 0.0)
    duration = np.maximum(sub[:, NUM_INDEX["duration"]], 0.0)
    iat_mean = duration / np.maximum(total_p - 1.0, 1.0)
    iat_std = np.maximum(sub[:, NUM_INDEX["iat_std"]], 0.0)
    sub[:, NUM_INDEX["iat_mean"]] = iat_mean
    sub[:, NUM_INDEX["iat_min"]] = np.maximum(iat_mean - 2.0 * iat_std, 0.0)
    sub[:, NUM_INDEX["iat_max"]] = iat_mean + 3.0 * iat_std
    mean_size = np.clip((orig_b + resp_b) / np.maximum(total_p, 1.0), 40.0, 1500.0)
    size_std = np.maximum(sub[:, NUM_INDEX["pkt_size_std"]], 0.0)
    sub[:, NUM_INDEX["pkt_size_mean"]] = mean_size
    sub[:, NUM_INDEX["pkt_size_min"]] = np.clip(mean_size - 1.8 * size_std, 40.0, 1500.0)
    sub[:, NUM_INDEX["pkt_size_max"]] = np.clip(mean_size + 2.2 * size_std, 40.0, 1500.0)
    sub[:, NUM_INDEX["payload_entropy"]] = np.clip(sub[:, NUM_INDEX["payload_entropy"]], 0.0, 4.5)
    mat[rows] = sub


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def _ip_pool(rng: np.random.Generator, prefix: str, size: int) -> list[str]:
    octets = rng.integers(1, 254, size=(size, 2))
    return [f"{prefix}.{a}.{b}" for a, b in octets]


class TrafficModel:
    """Draws batches of connection records from the class mixture.

    Sampling is columnar: one numpy block per class present in the batch, then a
    single pass to attach identifiers.  This is what lets one process feed the
    bus at tens of thousands of connections per second.
    """

    def __init__(
        self,
        seed: int = 1337,
        mix: dict[str, float] | None = None,
        drift: float = 0.0,
        hardness: float = DEFAULT_HARDNESS,
        hard_weight: tuple[float, float] = (0.2, 0.7),
        id_prefix: str = "f",
    ):
        self.rng = np.random.default_rng(seed)
        mix = dict(mix or DEFAULT_MIX)
        missing = set(CLASS_NAMES) - set(mix)
        for name in missing:
            mix[name] = 0.0
        total = sum(mix.values())
        if total <= 0:
            raise ValueError("class mix sums to zero")
        self.mix = {k: v / total for k, v in mix.items()}
        self.p = np.array([self.mix[name] for name in CLASS_NAMES], dtype=np.float64)
        self.drift = float(drift)
        #: Fraction of each batch pulled toward the opposite label (see above).
        self.hardness = float(hardness)
        self.hard_weight = hard_weight
        self.id_prefix = id_prefix
        self._counter = 0
        # Pre-rendered address pools: formatting IP strings per record would
        # otherwise dominate generation cost at high rates.
        self._internal = _ip_pool(self.rng, "10.4", 4096)
        self._external = _ip_pool(self.rng, "203.0", 4096)

    # -- internals -------------------------------------------------------
    def _apply_drift(self, mat: np.ndarray) -> None:
        """Perturb a batch to emulate a different network than the one trained
        on: volumes, durations and timing all shift by a lognormal factor."""
        if self.drift <= 0:
            return
        n = mat.shape[0]
        s = self.drift
        for name, sigma in (
            ("orig_bytes", 1.0), ("resp_bytes", 1.0), ("duration", 0.8),
            ("orig_pkts", 0.6), ("resp_pkts", 0.6), ("iat_mean", 0.8),
            ("iat_std", 0.9), ("pkt_size_mean", 0.3), ("pkt_size_std", 0.5),
            ("payload_entropy", 0.15),
        ):
            idx = NUM_INDEX[name]
            mat[:, idx] *= np.exp(self.rng.normal(0.0, s * sigma, n))
        # Timing stats must stay consistent with the perturbed durations.
        mat[:, NUM_INDEX["iat_min"]] = np.maximum(
            mat[:, NUM_INDEX["iat_mean"]] - 2 * mat[:, NUM_INDEX["iat_std"]], 0.0
        )
        mat[:, NUM_INDEX["iat_max"]] = (
            mat[:, NUM_INDEX["iat_mean"]] + 3 * mat[:, NUM_INDEX["iat_std"]]
        )

    def sample_matrix(self, n: int) -> tuple[np.ndarray, np.ndarray]:
        """Return ``(numeric_matrix, class_ids)`` for ``n`` flows, shuffled."""
        counts = self.rng.multinomial(n, self.p)
        blocks, ids = [], []
        for cid, (name, count) in enumerate(zip(CLASS_NAMES, counts)):
            if count == 0:
                continue
            blocks.append(SAMPLERS[name](self.rng, int(count)))
            ids.append(np.full(int(count), cid, dtype=np.int16))
        mat = np.vstack(blocks)
        class_ids = np.concatenate(ids)
        order = self.rng.permutation(mat.shape[0])
        mat, class_ids = mat[order], class_ids[order]
        labels = IS_ATTACK[class_ids]
        self._blend(mat, labels)
        self._apply_drift(mat)
        mat[:, NUM_INDEX["label"]] = labels
        return mat, class_ids

    def _blend(self, mat: np.ndarray, labels: np.ndarray) -> None:
        if self.hardness <= 0:
            return
        rng = self.rng
        attack_idx = np.flatnonzero(labels > 0.5)
        benign_idx = np.flatnonzero(labels <= 0.5)
        if attack_idx.size == 0 or benign_idx.size == 0:
            return
        lo, hi = self.hard_weight
        for rows, pool in ((attack_idx, benign_idx), (benign_idx, attack_idx)):
            pick = rows[rng.random(rows.size) < self.hardness]
            if pick.size == 0:
                continue
            partners = pool[rng.integers(0, pool.size, pick.size)]
            weights = rng.uniform(lo, hi, pick.size)
            _blend_rows(rng, mat, pick, partners, weights)

    def sample_records(self, n: int, now: float | None = None) -> tuple[list[tuple], np.ndarray]:
        """Return ``(record_tuples, class_ids)`` ready to publish."""
        import time

        mat, class_ids = self.sample_matrix(n)
        now = time.time() if now is None else now
        mat[:, NUM_INDEX["start_ts"]] = now
        mat[:, NUM_INDEX["emit_ts"]] = now
        rows = mat.tolist()  # one C-level conversion beats per-cell float() calls

        rng = self.rng
        n_rows = len(rows)
        src_idx = rng.integers(0, len(self._internal), n_rows)
        dst_idx = rng.integers(0, len(self._external), n_rows)
        # Attack sources are as often external as internal; benign east-west
        # traffic stays inside.  Kept simple: addresses are metadata, not
        # features, so the model can never key off them.
        internal, external = self._internal, self._external
        base = self._counter
        self._counter += n_rows
        prefix = self.id_prefix
        out = [
            (f"{prefix}{base + i:x}", internal[src_idx[i]], external[dst_idx[i]], *rows[i])
            for i in range(n_rows)
        ]
        return out, class_ids


def class_report(class_ids: np.ndarray, flagged: np.ndarray) -> dict[str, dict]:
    """Per-class detection breakdown used by the benchmark report."""
    out: dict[str, dict] = {}
    for cid, name in enumerate(CLASS_NAMES):
        mask = class_ids == cid
        total = int(mask.sum())
        if not total:
            continue
        hit = int(flagged[mask].sum())
        out[name] = {
            "count": total,
            "flagged": hit,
            "rate": hit / total,
            "is_attack": bool(IS_ATTACK[cid]),
        }
    return out
