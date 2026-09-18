"""Generate a realistic example network.

Used for the demo, the load tests and the MTTI harness.  It is not a random
graph: real networks have a shape that matters for how a map reads — a small
core, a distribution tier, access switches with tens of hosts hanging off each,
and departments that map onto subnets.  A uniform random graph would make the
visualisation look better than it deserves, because there would be no dense
regions to get lost in.

Deterministic for a given seed, so a benchmark or a visual-regression snapshot
compares like with like.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass

from cnmap.layout import compute_layout
from cnmap.models import DeviceType, Link, Node, Topology


@dataclass
class Department:
    name: str
    subnet_index: int
    host_share: float          # fraction of all hosts
    server_ratio: float        # of that department's hosts, how many are servers
    criticality: int
    tags: tuple[str, ...] = ()


DEPARTMENTS = (
    Department("finance", 10, 0.13, 0.10, 3, ("pci", "restricted")),
    Department("engineering", 20, 0.26, 0.22, 2, ("source-code",)),
    Department("sales", 30, 0.16, 0.05, 1, ()),
    Department("operations", 40, 0.12, 0.15, 2, ()),
    Department("datacenter", 50, 0.14, 0.92, 3, ("production", "restricted")),
    Department("dmz", 60, 0.04, 0.85, 3, ("internet-facing",)),
    Department("iot", 70, 0.09, 0.0, 1, ("unmanaged",)),
    Department("guest", 80, 0.06, 0.0, 0, ("untrusted",)),
)

WORKSTATION_VENDORS = ("Dell", "HewlettPackard", "Apple", "Intel", "Microsoft")
SERVER_VENDORS = ("Dell", "HewlettPackard", "VMware", "Oracle", "Intel")
NETWORK_VENDORS = ("Cisco", "Juniper", "Aruba", "Extreme")
IOT_VENDORS = ("RaspberryPi", "Espressif", "Philips", "Nest", "Polycom")
PRINTER_VENDORS = ("Canon", "Xerox", "Brother", "HewlettPackard")

WORKSTATION_OS = ("Windows 11 23H2", "Windows 10 22H2", "macOS 14.4", "Ubuntu 22.04")
SERVER_OS = ("Ubuntu 22.04 LTS", "RHEL 9.3", "Windows Server 2022", "VMware ESXi 8.0")

SERVER_SERVICES = (
    ["ssh", "https"], ["ssh", "http", "https"], ["ssh", "postgres"],
    ["smb", "rdp"], ["https", "dns"], ["ssh"],
)


def _mac(rng: random.Random, vendor: str) -> str:
    from .oui import BUILTIN_OUI

    prefixes = [p for p, v in BUILTIN_OUI.items() if v == vendor] or ["02FFFF"]
    prefix = rng.choice(prefixes)
    tail = "".join(f"{rng.randrange(256):02x}" for _ in range(3))
    joined = (prefix + tail).lower()
    return ":".join(joined[i:i + 2] for i in range(0, 12, 2))


def synthesize(
    n_hosts: int = 400,
    seed: int = 7,
    site: str = "hq",
    base_prefix: str = "10.20",
    include_wireless: bool = True,
) -> Topology:
    """Build a topology with roughly ``n_hosts`` end devices plus infrastructure."""
    rng = random.Random(seed)
    nodes: list[Node] = []
    links: list[Link] = []
    now = time.time()

    def add_node(**kwargs) -> Node:
        node = Node(first_seen=now, last_seen=now, site=site, **kwargs)
        nodes.append(node)
        return node

    def add_link(a: str, b: str, kind: str = "ethernet", bw: int | None = None,
                 by: str = "lldp") -> None:
        pair = tuple(sorted((a, b)))
        links.append(Link(id=f"l-{pair[0]}-{pair[1]}", source=pair[0], target=pair[1],
                          kind=kind, bandwidth_mbps=bw, discovered_by=[by]))

    # --- edge: firewall + core routers ----------------------------------
    firewall = add_node(
        id=f"n-{site}-fw-01", name=f"{site}-fw-01", ip=f"{base_prefix}.0.1",
        mac=_mac(rng, "Fortinet"), vendor="Fortinet", device_type=DeviceType.FIREWALL,
        os="FortiOS 7.4.1", subnet=f"{base_prefix}.0.0/24", criticality=3,
        tags=["infrastructure", "perimeter"], services=["https", "ssh"],
        discovered_by=["snmp", "lldp"],
    )
    cores = []
    for i in (1, 2):
        vendor = rng.choice(("Cisco", "Juniper"))
        core = add_node(
            id=f"n-{site}-core-{i:02d}", name=f"{site}-core-{i:02d}",
            ip=f"{base_prefix}.0.{10 + i}", mac=_mac(rng, vendor), vendor=vendor,
            device_type=DeviceType.ROUTER, os="IOS-XE 17.9" if vendor == "Cisco" else "Junos 22.4",
            subnet=f"{base_prefix}.0.0/24", criticality=3,
            tags=["infrastructure", "core"], services=["ssh", "snmp"],
            discovered_by=["snmp", "lldp"],
        )
        cores.append(core)
        add_link(firewall.id, core.id, "uplink", 10_000)
    add_link(cores[0].id, cores[1].id, "uplink", 40_000)

    # --- per-department access tier -------------------------------------
    host_targets = {d.name: max(int(n_hosts * d.host_share), 2) for d in DEPARTMENTS}
    dist_switches: list[Node] = []

    for dept in DEPARTMENTS:
        subnet = f"{base_prefix}.{dept.subnet_index}.0/24"
        target = host_targets[dept.name]
        # One access switch per ~48 hosts, the way a real wiring closet fills up.
        n_access = max(1, (target + 47) // 48)

        vendor = rng.choice(NETWORK_VENDORS)
        dist = add_node(
            id=f"n-{site}-{dept.name}-dist", name=f"{site}-{dept.name}-dist-01",
            ip=f"{base_prefix}.{dept.subnet_index}.2", mac=_mac(rng, vendor), vendor=vendor,
            device_type=DeviceType.SWITCH, os="IOS 15.2", subnet=subnet, criticality=3,
            tags=["infrastructure", "distribution", dept.name],
            services=["ssh", "snmp"], discovered_by=["snmp", "lldp"],
        )
        dist_switches.append(dist)
        for core in cores:
            add_link(core.id, dist.id, "uplink", 10_000)

        access_switches = []
        for a in range(n_access):
            vendor = rng.choice(NETWORK_VENDORS)
            sw = add_node(
                id=f"n-{site}-{dept.name}-acc-{a:02d}",
                name=f"{site}-{dept.name}-acc-{a + 1:02d}",
                ip=f"{base_prefix}.{dept.subnet_index}.{10 + a}", mac=_mac(rng, vendor),
                vendor=vendor, device_type=DeviceType.SWITCH, os="IOS 15.2",
                subnet=subnet, criticality=2, tags=["infrastructure", "access", dept.name],
                services=["ssh", "snmp"], discovered_by=["snmp", "lldp"],
            )
            access_switches.append(sw)
            add_link(dist.id, sw.id, "uplink", 1_000)

        if include_wireless and dept.name in {"sales", "engineering", "guest", "operations"}:
            vendor = rng.choice(("Aruba", "Cisco", "Ubiquiti"))
            ap = add_node(
                id=f"n-{site}-{dept.name}-ap-01", name=f"{site}-{dept.name}-ap-01",
                ip=f"{base_prefix}.{dept.subnet_index}.{40}", mac=_mac(rng, vendor),
                vendor=vendor, device_type=DeviceType.WIRELESS_AP, os="ArubaOS 8.10",
                subnet=subnet, criticality=2, tags=["infrastructure", "wireless", dept.name],
                services=["https"], discovered_by=["lldp"],
            )
            add_link(access_switches[0].id, ap.id, "uplink", 1_000)
            access_switches.append(ap)

        # --- hosts -------------------------------------------------------
        for h in range(target):
            host_ip = f"{base_prefix}.{dept.subnet_index}.{100 + h % 150}"
            if h >= 150:  # spill into a second /24 the way a big floor does
                host_ip = f"{base_prefix}.{dept.subnet_index + 1}.{100 + (h - 150) % 150}"
            roll = rng.random()
            if roll < dept.server_ratio:
                vendor = rng.choice(SERVER_VENDORS)
                node = add_node(
                    id=f"n-{site}-{dept.name}-srv-{h:03d}",
                    name=f"{site}-{dept.name}-srv-{h + 1:03d}", ip=host_ip,
                    mac=_mac(rng, vendor), vendor=vendor, device_type=DeviceType.SERVER,
                    os=rng.choice(SERVER_OS), subnet=subnet,
                    criticality=min(dept.criticality + 1, 3),
                    tags=[dept.name, *dept.tags], services=list(rng.choice(SERVER_SERVICES)),
                    discovered_by=["arp", "snmp"],
                )
            elif dept.name == "iot" or (roll > 0.96 and dept.name not in {"datacenter", "dmz"}):
                vendor = rng.choice(IOT_VENDORS)
                node = add_node(
                    id=f"n-{site}-{dept.name}-iot-{h:03d}",
                    name=f"{site}-{dept.name}-iot-{h + 1:03d}", ip=host_ip,
                    mac=_mac(rng, vendor), vendor=vendor, device_type=DeviceType.IOT,
                    os="embedded", subnet=subnet, criticality=max(dept.criticality - 1, 0),
                    tags=[dept.name, *dept.tags, "unmanaged"], services=["http"],
                    discovered_by=["arp"],
                )
            elif roll > 0.94:
                vendor = rng.choice(PRINTER_VENDORS)
                node = add_node(
                    id=f"n-{site}-{dept.name}-prn-{h:03d}",
                    name=f"{site}-{dept.name}-prn-{h + 1:03d}", ip=host_ip,
                    mac=_mac(rng, vendor), vendor=vendor, device_type=DeviceType.PRINTER,
                    os="printer firmware", subnet=subnet, criticality=0,
                    tags=[dept.name], services=["http", "ipp"], discovered_by=["arp", "snmp"],
                )
            else:
                vendor = rng.choice(WORKSTATION_VENDORS)
                node = add_node(
                    id=f"n-{site}-{dept.name}-ws-{h:03d}",
                    name=f"{site}-{dept.name}-ws-{h + 1:03d}", ip=host_ip,
                    mac=_mac(rng, vendor), vendor=vendor, device_type=DeviceType.WORKSTATION,
                    os=rng.choice(WORKSTATION_OS), subnet=subnet, criticality=dept.criticality,
                    tags=[dept.name, *dept.tags], services=[], discovered_by=["arp"],
                )
            node.ips = [node.ip]
            uplink = access_switches[h % len(access_switches)]
            wireless = uplink.device_type == DeviceType.WIRELESS_AP
            add_link(uplink.id, node.id, "wireless" if wireless else "ethernet",
                     1_000 if not wireless else 866, by="lldp" if not wireless else "inference:wireless")

    topology = Topology(nodes=nodes, links=links, version=1,
                        source=f"synthetic(seed={seed}, hosts={n_hosts})")
    return compute_layout(topology)


def main(argv=None) -> None:
    import argparse

    p = argparse.ArgumentParser(description="Generate an example network topology")
    p.add_argument("--nodes", type=int, default=400, help="approximate end-host count")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--site", default="hq")
    p.add_argument("--out", default="data/example-topology.json")
    args = p.parse_args(argv)

    topology = synthesize(args.nodes, args.seed, args.site)
    from pathlib import Path

    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(topology.model_dump_json(indent=2))
    stats = topology.stats()
    print(f"wrote {path}: {stats['nodes']} nodes, {stats['links']} links")
    print(f"  by type: {stats['by_type']}")
    print(f"  subnets: {len(stats['by_subnet'])}, vendors: {len(stats['vendors'])}")


if __name__ == "__main__":
    main()
