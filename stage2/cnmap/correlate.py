"""Map a Stage 1 alert onto a node in the topology.

Stage 1 emits a verdict about a *flow* — a five-tuple.  The map needs a *device*.
Bridging the two is the whole job of this module, and getting it wrong is worse
than useless: an alert pinned to the wrong host sends someone to the wrong desk.

Resolution order, most to least certain:

1. **exact IP** — the address is in the topology's address index;
2. **MAC** — if the alert carries one (Stage 1 does not today, but the hook is
   here because a Zeek or DHCP-aware exporter would);
3. **asset tag** — an external CMDB identifier carried on the alert;
4. **subnet** — no host matched, but the address falls inside a known subnet, so
   the alert attaches to that subnet's gateway and is labelled ``subnet`` so the
   UI can show it as approximate rather than precise;
5. **unresolved** — counted and exposed, never silently dropped.  An alert the
   map cannot place is an inventory gap, and hiding it hides the gap.

Which end of the flow is "the host in trouble": the internal endpoint, and when
both ends are internal, the initiator (source).  Stated here rather than assumed
because it is a judgement call, and ``prefer`` makes it configurable.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Optional

from .models import Alert, Severity, Topology

PRIVATE_NETS = tuple(
    ipaddress.ip_network(cidr) for cidr in
    ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
     "169.254.0.0/16", "fc00::/7", "fe80::/10")
)


def is_internal(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in PRIVATE_NETS)


@dataclass
class CorrelationResult:
    node_id: Optional[str] = None
    peer_node_id: Optional[str] = None
    method: str = "unresolved"
    unresolved_ip: Optional[str] = None


@dataclass
class Correlator:
    """Indexes a topology for fast alert attribution.

    Rebuilt whenever the topology changes; lookups are plain dict hits, because
    this runs on every alert at Stage 1's full rate.
    """

    by_ip: dict[str, str] = field(default_factory=dict)
    by_mac: dict[str, str] = field(default_factory=dict)
    by_tag: dict[str, str] = field(default_factory=dict)
    subnet_gateways: list[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, str]] = field(
        default_factory=list
    )
    prefer: str = "internal"
    unresolved: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_topology(cls, topology: Topology, prefer: str = "internal") -> "Correlator":
        self = cls(prefer=prefer)
        self.reindex(topology)
        return self

    def reindex(self, topology: Topology) -> None:
        self.by_ip.clear()
        self.by_mac.clear()
        self.by_tag.clear()
        self.subnet_gateways.clear()

        gateway_candidates: dict[str, tuple[int, str]] = {}
        for node in topology.nodes:
            for ip in ([node.ip] if node.ip else []) + list(node.ips):
                if ip:
                    self.by_ip.setdefault(ip, node.id)
            if node.mac:
                self.by_mac.setdefault(node.mac, node.id)
            for tag in node.tags:
                if tag.startswith("asset:"):
                    self.by_tag.setdefault(tag[len("asset:"):], node.id)
            # The most infrastructural device in a subnet stands in for it.
            rank = {"router": 3, "firewall": 3, "switch": 2, "wireless_ap": 1}.get(
                node.device_type.value, 0
            )
            if node.subnet and rank:
                current = gateway_candidates.get(node.subnet)
                if current is None or rank > current[0]:
                    gateway_candidates[node.subnet] = (rank, node.id)

        for subnet, (_rank, node_id) in gateway_candidates.items():
            try:
                self.subnet_gateways.append((ipaddress.ip_network(subnet, strict=False), node_id))
            except ValueError:
                continue

    # -- lookups ---------------------------------------------------------
    def node_for_ip(self, ip: str) -> tuple[Optional[str], str]:
        if not ip:
            return None, "unresolved"
        node_id = self.by_ip.get(ip)
        if node_id:
            return node_id, "ip"
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None, "unresolved"
        for network, gateway in self.subnet_gateways:
            if addr.version == network.version and addr in network:
                return gateway, "subnet"
        return None, "unresolved"

    def correlate(self, alert: Alert, mac: str | None = None, asset_tag: str | None = None) -> CorrelationResult:
        if mac and mac in self.by_mac:
            return CorrelationResult(node_id=self.by_mac[mac], method="mac")
        if asset_tag and asset_tag in self.by_tag:
            return CorrelationResult(node_id=self.by_tag[asset_tag], method="asset")

        src_node, src_method = self.node_for_ip(alert.src_ip)
        dst_node, dst_method = self.node_for_ip(alert.dst_ip)
        src_internal = is_internal(alert.src_ip)
        dst_internal = is_internal(alert.dst_ip)

        if self.prefer == "source":
            order = ((src_node, src_method, dst_node), (dst_node, dst_method, src_node))
        elif self.prefer == "destination":
            order = ((dst_node, dst_method, src_node), (src_node, src_method, dst_node))
        else:
            # "internal": whichever end is on our own network, source first when
            # both are - the initiator is the one behaving badly.
            if src_internal and src_node:
                order = ((src_node, src_method, dst_node), (dst_node, dst_method, src_node))
            elif dst_internal and dst_node:
                order = ((dst_node, dst_method, src_node), (src_node, src_method, dst_node))
            else:
                order = ((src_node, src_method, dst_node), (dst_node, dst_method, src_node))

        for node_id, method, peer in order:
            if node_id:
                return CorrelationResult(node_id=node_id, peer_node_id=peer, method=method)

        missing = alert.src_ip if src_internal or not dst_internal else alert.dst_ip
        self.unresolved[missing] = self.unresolved.get(missing, 0) + 1
        return CorrelationResult(method="unresolved", unresolved_ip=missing)

    def apply(self, alert: Alert, **kwargs) -> Alert:
        """Fill in ``node_id``/``peer_node_id``/``severity`` on an alert."""
        result = self.correlate(alert, **kwargs)
        alert.node_id = result.node_id
        alert.peer_node_id = result.peer_node_id
        alert.correlation = result.method
        alert.severity = Severity.from_score(alert.score)
        return alert

    def unresolved_report(self, top: int = 20) -> list[dict]:
        """Addresses the map could not place — an inventory gap, made visible."""
        rows = sorted(self.unresolved.items(), key=lambda kv: -kv[1])[:top]
        return [{"ip": ip, "alerts": count} for ip, count in rows]

    def stats(self) -> dict:
        return {
            "indexed_ips": len(self.by_ip),
            "indexed_macs": len(self.by_mac),
            "subnets": len(self.subnet_gateways),
            "unresolved_addresses": len(self.unresolved),
            "unresolved_alerts": sum(self.unresolved.values()),
        }
