"""Turn discovery observations into a topology.

The parsers deliberately know nothing about graphs; this is where sightings
become a device list with identities, subnets, vendors and edges.  Three jobs:

1. **Identity** — the same host seen by ARP, LLDP and SNMP must become one node.
   Merging is by MAC first (survives DHCP), then IP, then hostname.
2. **Classification** — device type and vendor from whatever hints the sources
   gave, with MAC OUI as the fallback.
3. **Edges** — LLDP and SNMP report real adjacency.  Hosts that only ever
   appeared in an ARP table have no known port, so they are attached to the
   switch serving their subnet and the edge is marked ``inferred`` rather than
   quietly presented as measured fact.
"""
from __future__ import annotations

import ipaddress
import time
from typing import Iterable, Optional

from cnmap.layout import compute_layout
from cnmap.models import DeviceType, Link, Node, Topology

from .observations import HostObservation, LinkObservation
from .oui import is_locally_administered, normalize_mac, vendor_of

INFRA_TYPES = {DeviceType.SWITCH, DeviceType.ROUTER, DeviceType.FIREWALL, DeviceType.WIRELESS_AP}


def subnet_of(ip: str | None, prefix: int = 24) -> str:
    if not ip:
        return "unknown"
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return "unknown"
    if addr.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))


def _node_id(obs: HostObservation) -> str:
    mac = normalize_mac(obs.mac)
    if mac and not is_locally_administered(mac):
        return "n-" + mac.replace(":", "")
    if obs.ip:
        return "n-ip-" + obs.ip.replace(".", "-").replace(":", "-")
    if mac:
        return "n-" + mac.replace(":", "")
    return "n-" + (obs.hostname or "unknown").lower().replace(" ", "-")


def _device_type(obs: HostObservation, vendor: str) -> DeviceType:
    hint = (obs.device_type_hint or "").strip().lower()
    if hint:
        try:
            return DeviceType(hint)
        except ValueError:
            pass
    descr = f"{obs.os_hint} {obs.hostname}".lower()
    for needle, dtype in (
        ("firewall", DeviceType.FIREWALL), ("-fw", DeviceType.FIREWALL),
        ("router", DeviceType.ROUTER), ("-rtr", DeviceType.ROUTER), ("gw", DeviceType.ROUTER),
        ("switch", DeviceType.SWITCH), ("-sw", DeviceType.SWITCH),
        ("ap-", DeviceType.WIRELESS_AP), ("wap", DeviceType.WIRELESS_AP),
        ("printer", DeviceType.PRINTER), ("laserjet", DeviceType.PRINTER),
        ("server", DeviceType.SERVER), ("srv", DeviceType.SERVER),
    ):
        if needle in descr:
            return dtype
    # Vendor is a weak last resort, but a Raspberry Pi or an Espressif module on
    # a corporate network is far more likely to be IoT than a workstation.
    if vendor in {"RaspberryPi", "Espressif", "Philips", "Nest"}:
        return DeviceType.IOT
    if vendor in {"Canon", "Xerox", "Brother"}:
        return DeviceType.PRINTER
    if vendor in {"Cisco", "Juniper", "Aruba", "Extreme", "Ubiquiti"}:
        return DeviceType.SWITCH
    if vendor == "Fortinet":
        return DeviceType.FIREWALL
    return DeviceType.UNKNOWN


def build_nodes(observations: Iterable[HostObservation], prefix: int = 24) -> list[Node]:
    """Merge observations into nodes."""
    merged: dict[str, HostObservation] = {}
    order: list[str] = []
    # Second pass index so an SNMP sighting with only a hostname can join an ARP
    # sighting that only had a MAC.
    by_hostname: dict[str, str] = {}

    for obs in observations:
        key = obs.key()
        host_key = (obs.hostname or "").strip().lower()
        if key not in merged and host_key and host_key in by_hostname:
            key = by_hostname[host_key]
        existing = merged.get(key)
        if existing is None:
            merged[key] = obs
            order.append(key)
            if host_key:
                by_hostname.setdefault(host_key, key)
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
        sources = set(existing.source.split("+")) | set(obs.source.split("+"))
        existing.source = "+".join(sorted(s for s in sources if s))
        extra_ips = set(existing.extra.get("ips", [])) | set(obs.extra.get("ips", []))
        if obs.ip and obs.ip != existing.ip:
            extra_ips.add(obs.ip)
        if extra_ips:
            existing.extra["ips"] = sorted(extra_ips)
        existing.extra.update({k: v for k, v in obs.extra.items() if k != "ips"})
        if host_key:
            by_hostname.setdefault(host_key, key)

    now = time.time()
    nodes: list[Node] = []
    for key in order:
        obs = merged[key]
        mac = normalize_mac(obs.mac)
        vendor = obs.vendor_hint or vendor_of(mac)
        device_type = _device_type(obs, vendor)
        ips = [obs.ip] if obs.ip else []
        ips += [ip for ip in obs.extra.get("ips", []) if ip != obs.ip]
        tags = []
        if mac and is_locally_administered(mac):
            tags.append("randomized-mac")
        if device_type in INFRA_TYPES:
            tags.append("infrastructure")
        nodes.append(Node(
            id=_node_id(obs),
            name=obs.hostname or obs.ip or (mac or "unknown"),
            ip=obs.ip, ips=ips, mac=mac, vendor=vendor, device_type=device_type,
            os=obs.os_hint, subnet=subnet_of(obs.ip, prefix),
            services=list(obs.services),
            discovered_by=sorted({s for s in obs.source.split("+") if s}),
            first_seen=now, last_seen=now,
            tags=tags,
        ))
    return nodes


def _resolve(endpoint: str, mac: Optional[str], nodes: list[Node]) -> Optional[str]:
    """Map an LLDP/SNMP endpoint name onto a node id."""
    mac = normalize_mac(mac)
    if mac:
        for node in nodes:
            if node.mac == mac:
                return node.id
    if not endpoint:
        return None
    lowered = endpoint.strip().lower()
    for node in nodes:
        if node.name.lower() == lowered:
            return node.id
        if node.ip == endpoint or endpoint in node.ips:
            return node.id
    mac_endpoint = normalize_mac(endpoint)
    if mac_endpoint:
        for node in nodes:
            if node.mac == mac_endpoint:
                return node.id
    return None


def build_links(
    observations: Iterable[LinkObservation], nodes: list[Node], infer_host_uplinks: bool = True
) -> list[Link]:
    links: list[Link] = []
    seen: set[tuple[str, str]] = set()

    for obs in observations:
        source = _resolve(obs.local, obs.local_mac, nodes)
        target = _resolve(obs.remote, obs.remote_mac, nodes)
        if not source or not target or source == target:
            continue
        pair = tuple(sorted((source, target)))
        if pair in seen:
            continue
        seen.add(pair)
        links.append(Link(
            id=f"l-{pair[0]}-{pair[1]}", source=pair[0], target=pair[1],
            kind=obs.kind, bandwidth_mbps=obs.bandwidth_mbps,
            discovered_by=[obs.source],
        ))

    if not infer_host_uplinks:
        return links

    # Anything with no edge and no port information gets attached to the switch
    # serving its subnet.  Marked `inferred`, because it is a guess: ARP proves
    # the host is in the broadcast domain, not which port it is on.
    connected = {n for pair in seen for n in pair}
    switch_for_subnet: dict[str, str] = {}
    for node in nodes:
        if node.device_type in {DeviceType.SWITCH, DeviceType.WIRELESS_AP}:
            switch_for_subnet.setdefault(node.subnet, node.id)
    fallback_switch = next(
        (n.id for n in nodes if n.device_type in {DeviceType.SWITCH, DeviceType.ROUTER}), None
    )
    for node in nodes:
        if node.id in connected or node.device_type in INFRA_TYPES:
            continue
        uplink = switch_for_subnet.get(node.subnet) or fallback_switch
        if not uplink or uplink == node.id:
            continue
        pair = tuple(sorted((node.id, uplink)))
        if pair in seen:
            continue
        seen.add(pair)
        links.append(Link(
            id=f"l-{pair[0]}-{pair[1]}", source=pair[0], target=pair[1],
            kind="inferred", discovered_by=["inference:subnet"],
        ))
    return links


def build_topology(
    hosts: Iterable[HostObservation],
    link_observations: Iterable[LinkObservation] = (),
    prefix: int = 24,
    source: str = "discovery",
    infer_host_uplinks: bool = True,
) -> Topology:
    nodes = build_nodes(hosts, prefix)
    links = build_links(link_observations, nodes, infer_host_uplinks)
    topology = Topology(nodes=nodes, links=links, source=source, version=1)
    return compute_layout(topology)
