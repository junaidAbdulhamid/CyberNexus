"""Active discovery.

Everything here sends packets, so everything here is opt-in and off by default.
Passive sources (ARP cache, LLDP from the local daemon) tell you a great deal
without touching the network; active probing is for filling the gaps, and on a
production network it needs authorisation before it needs code.

Three probes, each degrading gracefully when its dependency or privilege is
missing — a discovery run should return less, never crash:

* **ARP sweep** (scapy, needs root/CAP_NET_RAW) — finds hosts that have not
  spoken recently enough to be in anyone's cache.
* **SNMP get/walk** (pysnmp if installed, else the ``snmpwalk`` binary) — the
  high-yield one; parsing lives in ``snmp.py`` so it is testable without a
  network.
* **TCP service probe** — a plain connect() to a short list of ports, purely to
  label a node ("this is an SSH server").  Not a port scanner: no SYN scanning,
  no wide ranges, and a strict concurrency cap.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

from .observations import HostObservation
from .oui import normalize_mac
from .snmp import parse_snmp_device

log = logging.getLogger(__name__)

#: Ports probed for service labelling.  Deliberately short and boring.
DEFAULT_SERVICE_PORTS = (22, 53, 80, 135, 139, 443, 445, 3389, 8080, 161)
PORT_NAMES = {
    22: "ssh", 53: "dns", 80: "http", 135: "msrpc", 139: "netbios",
    443: "https", 445: "smb", 3389: "rdp", 8080: "http-alt", 161: "snmp",
}


def scapy_available() -> bool:
    try:
        import scapy.all  # noqa: F401

        return True
    except Exception:
        return False


def arp_sweep(cidr: str, timeout: float = 2.0, retry: int = 1) -> list[HostObservation]:
    """Broadcast ARP requests across ``cidr`` and collect the replies.

    Returns an empty list (with a warning) rather than raising when scapy is
    missing or the process lacks CAP_NET_RAW, so a discovery run on an
    unprivileged box still completes with its passive sources.
    """
    if not scapy_available():
        log.warning("arp_sweep: scapy is not installed; skipping active sweep")
        return []
    try:
        from scapy.layers.l2 import ARP, Ether
        from scapy.sendrecv import srp

        network = ipaddress.ip_network(cidr, strict=False)
        if network.num_addresses > 4096:
            raise ValueError(f"{cidr} is too large to sweep ({network.num_addresses} addresses)")
        packet = Ether(dst="ff:ff:ff:ff:ff:ff") / ARP(pdst=str(network))
        answered, _ = srp(packet, timeout=timeout, retry=retry, verbose=False)
    except PermissionError:
        log.warning("arp_sweep: needs root / CAP_NET_RAW; skipping")
        return []
    except Exception as exc:  # pragma: no cover - environment dependent
        log.warning("arp_sweep failed: %s", exc)
        return []

    out = []
    for _sent, received in answered:
        out.append(HostObservation(
            ip=received.psrc, mac=normalize_mac(received.hwsrc),
            source="arp:active",
        ))
    return out


def snmp_walk_command(host: str, community: str = "public", version: str = "2c",
                      oid: str = "", timeout: float = 5.0) -> str:
    """Run the system ``snmpwalk``.  Returns '' when unavailable."""
    cmd = ["snmpwalk", f"-v{version}", "-c", community, "-t", str(int(timeout)), host]
    if oid:
        cmd.append(oid)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout * 4)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def snmp_walk_pysnmp(host: str, community: str = "public", timeout: float = 3.0) -> str:
    """Walk via pysnmp when installed, rendered in snmpwalk's text format so the
    same parser handles both paths."""
    try:
        from pysnmp.hlapi import (  # type: ignore
            CommunityData, ContextData, ObjectIdentity, ObjectType, SnmpEngine,
            UdpTransportTarget, nextCmd,
        )
    except Exception:
        return ""
    lines = []
    try:
        iterator = nextCmd(
            SnmpEngine(), CommunityData(community),
            UdpTransportTarget((host, 161), timeout=timeout, retries=1),
            ContextData(),
            ObjectType(ObjectIdentity("1.3.6.1.2.1.1")),      # system
            ObjectType(ObjectIdentity("1.3.6.1.2.1.2.2.1")),  # ifTable
            ObjectType(ObjectIdentity("1.3.6.1.2.1.4.22.1")), # ipNetToMedia
            lexicographicMode=False,
        )
        for error_indication, error_status, _idx, var_binds in iterator:
            if error_indication or error_status:
                break
            for var_bind in var_binds:
                lines.append(f"{var_bind[0].prettyPrint()} = STRING: {var_bind[1].prettyPrint()}")
    except Exception as exc:  # pragma: no cover - network dependent
        log.debug("pysnmp walk of %s failed: %s", host, exc)
    return "\n".join(lines)


def snmp_probe(hosts: Iterable[str], community: str = "public", workers: int = 8):
    """Walk several devices; returns (devices, learned hosts, links)."""
    devices, neighbours, links = [], [], []

    def one(host: str):
        text = snmp_walk_pysnmp(host, community) or snmp_walk_command(host, community)
        if not text.strip():
            return None
        return parse_snmp_device(text, host)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(one, list(hosts)):
            if not result:
                continue
            device, hosts_seen, device_links = result
            devices.append(device)
            neighbours.extend(hosts_seen)
            links.extend(device_links)
    return devices, neighbours, links


def probe_services(ip: str, ports: Iterable[int] = DEFAULT_SERVICE_PORTS,
                   timeout: float = 0.4) -> list[str]:
    """Label a host by which of a few well-known ports accept a connection."""
    found = []
    for port in ports:
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                found.append(PORT_NAMES.get(port, str(port)))
        except OSError:
            continue
    return found


def enrich_with_services(observations: list[HostObservation], workers: int = 16,
                         ports: Iterable[int] = DEFAULT_SERVICE_PORTS) -> list[HostObservation]:
    targets = [obs for obs in observations if obs.ip]

    def one(obs: HostObservation) -> HostObservation:
        obs.services = probe_services(obs.ip, ports)
        return obs

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, targets))
    return observations


def resolve_hostnames(observations: list[HostObservation], workers: int = 16) -> list[HostObservation]:
    """Reverse-DNS anything still unnamed."""
    def one(obs: HostObservation) -> None:
        if obs.hostname or not obs.ip:
            return
        try:
            obs.hostname = socket.gethostbyaddr(obs.ip)[0]
        except OSError:
            pass

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, observations))
    return observations
