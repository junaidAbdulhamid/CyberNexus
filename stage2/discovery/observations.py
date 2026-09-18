"""The intermediate type every discovery source produces.

Each source (ARP, LLDP, SNMP, active probe) parses its own wire format into
these, and ``discovery/build.py`` merges them into a ``Topology``.  Keeping the
parsers free of topology-building logic is what lets every one of them be
tested against captured real-world output with no network involved.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class HostObservation:
    """"I saw a device." """

    ip: Optional[str] = None
    mac: Optional[str] = None
    hostname: str = ""
    interface: str = ""
    source: str = "unknown"
    device_type_hint: str = ""
    vendor_hint: str = ""
    os_hint: str = ""
    services: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def key(self) -> str:
        """Identity for merging: MAC when we have one, else IP, else hostname.

        MAC first because it survives DHCP; IP is the fallback for anything
        behind a router, where we will never see the real hardware address.
        """
        from .oui import normalize_mac

        mac = normalize_mac(self.mac)
        if mac:
            return f"mac:{mac}"
        if self.ip:
            return f"ip:{self.ip}"
        return f"host:{self.hostname.lower()}"


@dataclass
class LinkObservation:
    """"These two devices are directly connected." """

    local: str                 # hostname / chassis id / ip of the reporting device
    remote: str                # hostname / chassis id / ip of the neighbour
    local_port: str = ""
    remote_port: str = ""
    source: str = "unknown"
    kind: str = "ethernet"
    bandwidth_mbps: Optional[int] = None
    local_mac: Optional[str] = None
    remote_mac: Optional[str] = None
