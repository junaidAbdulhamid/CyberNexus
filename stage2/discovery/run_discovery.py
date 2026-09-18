"""Discovery CLI: build a topology from whatever the network will tell us.

Passive sources run by default and send no packets. Active probing is opt-in,
one flag per technique, because on someone else's network an ARP sweep is
something you ask permission for first.

    # passive only: this host's ARP cache and LLDP neighbours
    python -m discovery.run_discovery

    # add SNMP against known infrastructure (read-only community)
    python -m discovery.run_discovery --snmp 10.0.0.1,10.0.0.2 --community public

    # add an ARP sweep of a subnet (needs root) and reverse DNS
    sudo -E python -m discovery.run_discovery --sweep 10.0.1.0/24 --resolve

    # parse captured command output instead of running anything
    python -m discovery.run_discovery --arp-file arp.txt --lldp-file lldp.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from cnmap.models import Topology

from .arp import parse_arp_output, read_local_arp_table
from .build import build_topology
from .lldp import parse_lldp_json, parse_lldp_text, read_local_lldp
from .observations import HostObservation, LinkObservation
from .probe import arp_sweep, enrich_with_services, resolve_hostnames, snmp_probe

log = logging.getLogger("discovery")


def collect(args) -> tuple[list[HostObservation], list[LinkObservation], dict]:
    hosts: list[HostObservation] = []
    links: list[LinkObservation] = []
    sources: dict[str, int] = {}

    def note(name: str, found: int) -> None:
        sources[name] = sources.get(name, 0) + found
        log.info("%-18s %d observation(s)", name, found)

    # --- passive ---------------------------------------------------------
    if args.arp_file:
        found = parse_arp_output(Path(args.arp_file).read_text())
        hosts.extend(found)
        note("arp (file)", len(found))
    elif not args.no_local_arp:
        found = read_local_arp_table()
        hosts.extend(found)
        note("arp (local)", len(found))

    if args.lldp_file:
        text = Path(args.lldp_file).read_text()
        parser = parse_lldp_json if text.lstrip().startswith("{") else parse_lldp_text
        found_hosts, found_links = parser(text, args.local_name)
        hosts.extend(found_hosts)
        links.extend(found_links)
        note("lldp (file)", len(found_hosts))
    elif not args.no_local_lldp:
        found_hosts, found_links = read_local_lldp(local_name=args.local_name)
        hosts.extend(found_hosts)
        links.extend(found_links)
        note("lldp (local)", len(found_hosts))

    # --- active (opt-in) -------------------------------------------------
    if args.snmp:
        targets = [t.strip() for t in args.snmp.split(",") if t.strip()]
        devices, neighbours, snmp_links = snmp_probe(targets, args.community)
        hosts.extend(devices)
        hosts.extend(neighbours)
        links.extend(snmp_links)
        note("snmp", len(devices) + len(neighbours))

    if args.sweep:
        for cidr in args.sweep.split(","):
            found = arp_sweep(cidr.strip(), timeout=args.timeout)
            hosts.extend(found)
            note(f"arp sweep {cidr.strip()}", len(found))

    if args.services:
        enrich_with_services(hosts)
        note("service probe", sum(1 for h in hosts if h.services))

    if args.resolve:
        before = sum(1 for h in hosts if h.hostname)
        resolve_hostnames(hosts)
        note("reverse dns", sum(1 for h in hosts if h.hostname) - before)

    return hosts, links, sources


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build a network topology by discovery")
    parser.add_argument("--out", default="data/discovered-topology.json")
    parser.add_argument("--local-name", default="localhost",
                        help="name for this host in LLDP-derived links")
    parser.add_argument("--prefix", type=int, default=24, help="subnet mask length for grouping")

    passive = parser.add_argument_group("passive sources (default, no packets sent)")
    passive.add_argument("--arp-file", help="parse saved `arp -a` / `ip neigh` output")
    passive.add_argument("--lldp-file", help="parse saved lldpcli output (JSON or text)")
    passive.add_argument("--no-local-arp", action="store_true")
    passive.add_argument("--no-local-lldp", action="store_true")

    active = parser.add_argument_group("active probing (opt-in, sends packets)")
    active.add_argument("--snmp", help="comma-separated devices to walk")
    active.add_argument("--community", default="public", help="SNMP v2c community")
    active.add_argument("--sweep", help="comma-separated CIDRs to ARP-sweep (needs root)")
    active.add_argument("--services", action="store_true",
                        help="TCP connect to a few well-known ports to label hosts")
    active.add_argument("--resolve", action="store_true", help="reverse-DNS unnamed hosts")
    active.add_argument("--timeout", type=float, default=2.0)

    parser.add_argument("--no-infer-links", action="store_true",
                        help="do not attach hosts to their subnet switch")
    parser.add_argument("--merge", help="merge into an existing topology JSON")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")

    started = time.time()
    hosts, links, sources = collect(args)

    if args.merge and Path(args.merge).exists():
        previous = Topology.model_validate_json(Path(args.merge).read_text())
        for node in previous.nodes:
            hosts.append(HostObservation(
                ip=node.ip, mac=node.mac, hostname=node.name,
                source="+".join(node.discovered_by) or "previous",
                device_type_hint=node.device_type.value, vendor_hint=node.vendor,
                os_hint=node.os, services=list(node.services),
            ))
        log.info("merged %d node(s) from %s", len(previous.nodes), args.merge)

    if not hosts:
        log.error("no devices discovered. On a laptop the ARP cache is often nearly "
                  "empty; try --sweep <cidr> (needs root) or --snmp <router>, or "
                  "generate an example network with `python -m discovery.synthesize`.")
        return 1

    topology = build_topology(hosts, links, prefix=args.prefix, source="discovery",
                              infer_host_uplinks=not args.no_infer_links)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(topology.model_dump_json(indent=2))

    stats = topology.stats()
    measured = sum(1 for l in topology.links if l.kind != "inferred")
    print(json.dumps({
        "output": str(out),
        "elapsed_s": round(time.time() - started, 2),
        "sources": sources,
        "nodes": stats["nodes"],
        "links": stats["links"],
        "links_measured": measured,
        "links_inferred": stats["links"] - measured,
        "by_type": stats["by_type"],
        "subnets": len(stats["by_subnet"]),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
