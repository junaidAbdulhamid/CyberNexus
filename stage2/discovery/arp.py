"""ARP / neighbour table parsing.

The ARP cache is the cheapest discovery there is: it costs no packets, it is
already populated on every host, and it is the only source that reliably ties an
IP to a MAC — which is what alert correlation needs when Stage 1 reports a
five-tuple and the map has to decide which box that was.

Four formats, because the same information is spelled differently everywhere:

* BSD/macOS  ``? (10.0.0.1) at ab:cd:ef:12:34:56 on en0 ifscope [ethernet]``
* Linux arp  ``gw (10.0.0.1) at ab:cd:ef:12:34:56 [ether] on eth0``
* iproute2   ``10.0.0.1 dev eth0 lladdr ab:cd:ef:12:34:56 REACHABLE``
* procfs     ``/proc/net/arp`` columns
"""
from __future__ import annotations

import ipaddress
import re
import subprocess
from typing import Iterable

from .observations import HostObservation
from .oui import normalize_mac

SOURCE = "arp"

# "? (10.0.0.1) at ab:cd:ef:12:34:56 on en0"  /  "name (ip) at mac [ether] on eth0"
_PAREN = re.compile(
    r"^(?P<host>\S*)\s*\((?P<ip>[0-9a-fA-F:.]+)\)\s+at\s+(?P<mac>[0-9a-fA-F:.\-]+|<incomplete>|\(incomplete\))"
    r"(?:\s+\[[^\]]*\])?"
    r"(?:\s+on\s+(?P<iface>\S+))?",
)
# "10.0.0.1 dev eth0 lladdr ab:cd:ef:12:34:56 REACHABLE"
_IPNEIGH = re.compile(
    r"^(?P<ip>[0-9a-fA-F:.]+)\s+dev\s+(?P<iface>\S+)(?:\s+lladdr\s+(?P<mac>[0-9a-fA-F:.\-]+))?"
    r"(?:.*?\b(?P<state>PERMANENT|NOARP|REACHABLE|STALE|DELAY|PROBE|FAILED|INCOMPLETE))?",
)

_INCOMPLETE = {"<incomplete>", "(incomplete)", "incomplete", ""}


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def parse_arp_output(text: str) -> list[HostObservation]:
    """Parse any of the supported neighbour-table formats.

    Entries with no resolved hardware address are skipped: an incomplete ARP
    entry means the address did not answer, and inventing a device for it fills
    the map with ghosts.
    """
    out: list[HostObservation] = []
    seen: set[tuple[str | None, str | None]] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("IP address", "Address ")):
            continue

        mac = ip = iface = None
        hostname = ""

        m = _PAREN.match(line)
        if m:
            ip = m.group("ip")
            mac_raw = (m.group("mac") or "").strip().lower()
            mac = None if mac_raw in _INCOMPLETE else mac_raw
            iface = m.group("iface") or ""
            host = m.group("host") or ""
            hostname = "" if host in {"?", ""} else host
        else:
            m = _IPNEIGH.match(line)
            if m:
                ip, mac, iface = m.group("ip"), m.group("mac"), m.group("iface") or ""
                if m.group("state") in {"FAILED", "INCOMPLETE"}:
                    continue
            else:
                # /proc/net/arp: IP  HWtype  Flags  HWaddress  Mask  Device
                parts = line.split()
                if len(parts) >= 6 and _valid_ip(parts[0]):
                    ip, mac, iface = parts[0], parts[3], parts[5]
                    if parts[2] == "0x0":  # incomplete flag
                        continue
                else:
                    continue

        mac = normalize_mac(mac)
        if not mac or not ip or not _valid_ip(ip):
            continue
        if (ip, mac) in seen:
            continue
        seen.add((ip, mac))
        out.append(HostObservation(
            ip=ip, mac=mac, hostname=hostname, interface=iface or "", source=SOURCE,
        ))
    return out


def parse_proc_net_arp(text: str) -> list[HostObservation]:
    """``/proc/net/arp`` specifically (header row plus fixed columns)."""
    lines = [l for l in text.splitlines() if l.strip()]
    if lines and lines[0].lstrip().startswith("IP address"):
        lines = lines[1:]
    return parse_arp_output("\n".join(lines))


def read_local_arp_table(timeout: float = 5.0) -> list[HostObservation]:
    """Read this host's neighbour table.  Passive: no packets are sent."""
    for cmd in (["ip", "neigh", "show"], ["arp", "-an"], ["arp", "-a"]):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            return parse_arp_output(proc.stdout)
    try:
        with open("/proc/net/arp") as fh:
            return parse_proc_net_arp(fh.read())
    except OSError:
        return []


def merge_observations(batches: Iterable[list[HostObservation]]) -> list[HostObservation]:
    """Collapse repeated sightings of the same host, newest metadata winning."""
    merged: dict[str, HostObservation] = {}
    for batch in batches:
        for obs in batch:
            key = obs.key()
            existing = merged.get(key)
            if existing is None:
                merged[key] = obs
                continue
            existing.ip = existing.ip or obs.ip
            existing.mac = existing.mac or obs.mac
            existing.hostname = existing.hostname or obs.hostname
            existing.interface = existing.interface or obs.interface
            existing.device_type_hint = existing.device_type_hint or obs.device_type_hint
            existing.vendor_hint = existing.vendor_hint or obs.vendor_hint
            existing.os_hint = existing.os_hint or obs.os_hint
            for svc in obs.services:
                if svc not in existing.services:
                    existing.services.append(svc)
            if obs.source not in existing.source.split("+"):
                existing.source = f"{existing.source}+{obs.source}"
            existing.extra.update(obs.extra)
    return list(merged.values())
