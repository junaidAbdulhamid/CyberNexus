"""Topology building, layout and correlation."""
import pytest

from cnmap.correlate import Correlator, is_internal
from cnmap.layout import bounds, compute_layout
from cnmap.models import Alert, DeviceType, Severity, Topology
from discovery.arp import parse_arp_output
from discovery.build import build_links, build_nodes, build_topology, subnet_of
from discovery.lldp import parse_lldp_text
from discovery.observations import HostObservation, LinkObservation
from discovery.snmp import parse_snmp_device
from discovery.synthesize import synthesize


class TestBuild:
    def test_one_host_seen_by_three_sources_becomes_one_node(self):
        """The whole point of merging: ARP knows the MAC, LLDP knows the name,
        SNMP knows the type. They are the same switch."""
        arp = parse_arp_output("core-sw-01 (10.0.0.2) at 0:1b:54:11:22:33 on en0")
        lldp_hosts, lldp_links = parse_lldp_text(
            "Interface: eth0, via: LLDP\n"
            "  Chassis:\n"
            "    ChassisID:    mac 00:1b:54:11:22:33\n"
            "    SysName:      core-sw-01\n"
            "    Capability:   Bridge, on\n"
            "  Port:\n"
            "    PortID:       ifname Gi0/1\n", local_name="10.0.0.9")
        snmp_device, _, _ = parse_snmp_device(
            "SNMPv2-MIB::sysName.0 = STRING: core-sw-01\n"
            "SNMPv2-MIB::sysDescr.0 = STRING: Cisco Catalyst\n"
            "IF-MIB::ifPhysAddress.1 = STRING: 0:1b:54:11:22:33\n", "10.0.0.2")

        nodes = build_nodes(list(arp) + lldp_hosts + [snmp_device])
        assert len(nodes) == 1
        node = nodes[0]
        assert node.name == "core-sw-01"
        assert node.ip == "10.0.0.2"
        assert node.device_type is DeviceType.SWITCH
        assert node.vendor == "Cisco"
        assert set(node.discovered_by) == {"arp", "lldp", "snmp"}

    def test_hosts_without_a_mac_key_on_ip(self):
        nodes = build_nodes([
            HostObservation(ip="10.0.0.5", source="snmp"),
            HostObservation(ip="10.0.0.5", hostname="srv", source="snmp"),
        ])
        assert len(nodes) == 1 and nodes[0].name == "srv"

    def test_randomised_macs_are_flagged_and_keyed_on_ip(self):
        nodes = build_nodes([HostObservation(ip="10.0.0.9", mac="02:11:22:33:44:55", source="arp")])
        assert "randomized-mac" in nodes[0].tags
        assert nodes[0].id.startswith("n-ip-")

    def test_measured_and_inferred_links_are_distinguished(self):
        """ARP proves a host is in the broadcast domain, not which port it is
        on. Presenting that guess as a measured adjacency would be a lie."""
        nodes = build_nodes([
            HostObservation(ip="10.0.0.2", mac="00:1b:54:11:22:33", hostname="sw1",
                            device_type_hint="switch", source="lldp"),
            HostObservation(ip="10.0.0.10", mac="00:0c:29:11:11:11", source="arp"),
            HostObservation(ip="10.0.0.11", mac="00:0c:29:22:22:22", source="arp"),
        ])
        links = build_links([LinkObservation(local="sw1", remote="10.0.0.10", source="lldp")], nodes)
        kinds = {l.kind for l in links}
        assert "ethernet" in kinds and "inferred" in kinds
        inferred = [l for l in links if l.kind == "inferred"]
        assert all(l.discovered_by == ["inference:subnet"] for l in inferred)

    def test_inference_can_be_switched_off(self):
        nodes = build_nodes([HostObservation(ip="10.0.0.10", mac="00:0c:29:11:11:11", source="arp")])
        assert build_links([], nodes, infer_host_uplinks=False) == []

    @pytest.mark.parametrize("ip,expected", [
        ("10.20.30.40", "10.20.30.0/24"),
        ("192.168.1.9", "192.168.1.0/24"),
        (None, "unknown"),
        ("not-an-ip", "unknown"),
    ])
    def test_subnet_derivation(self, ip, expected):
        assert subnet_of(ip) == expected

    def test_build_topology_is_laid_out(self):
        topology = build_topology([
            HostObservation(ip=f"10.0.0.{i}", mac=f"00:0c:29:00:00:{i:02x}", source="arp")
            for i in range(2, 20)
        ])
        assert len(topology.nodes) == 18
        assert any(n.position.x != 0 or n.position.z != 0 for n in topology.nodes)


class TestLayout:
    def test_deterministic(self):
        a = synthesize(80, seed=3)
        b = synthesize(80, seed=3)
        assert [(n.id, n.position.x, n.position.y) for n in a.nodes] == \
               [(n.id, n.position.x, n.position.y) for n in b.nodes]

    def test_layers_stack_by_role(self):
        topology = synthesize(120, seed=5)
        by_type = {}
        for node in topology.nodes:
            by_type.setdefault(node.device_type, []).append(node.position.y)
        hosts = by_type[DeviceType.WORKSTATION]
        routers = by_type[DeviceType.ROUTER]
        assert max(hosts) < min(routers), "infrastructure must sit above hosts"

    def test_hosts_of_a_subnet_cluster_together(self):
        topology = synthesize(200, seed=11)
        groups = {}
        for node in topology.nodes:
            if node.layer == 0:
                groups.setdefault(node.subnet, []).append(node.position)
        spreads = []
        for positions in groups.values():
            if len(positions) < 5:
                continue
            xs = [p.x for p in positions]
            zs = [p.z for p in positions]
            spreads.append(max(max(xs) - min(xs), max(zs) - min(zs)))
        overall = bounds(topology.nodes)["radius"] * 2
        assert max(spreads) < overall, "a district must be tighter than the whole map"

    def test_bounds_are_sane(self):
        b = bounds(synthesize(60, seed=2).nodes)
        assert b["radius"] > 0
        assert b["min"]["y"] <= b["center"]["y"] <= b["max"]["y"]

    def test_empty_topology_does_not_crash(self):
        assert compute_layout(Topology()).nodes == []


class TestCorrelation:
    @pytest.fixture
    def topology(self):
        return synthesize(120, seed=13)

    def test_exact_ip_match(self, topology):
        host = next(n for n in topology.nodes if n.device_type is DeviceType.WORKSTATION)
        correlator = Correlator.from_topology(topology)
        alert = correlator.apply(Alert(src_ip=host.ip, dst_ip="203.0.113.1", score=0.97))
        assert alert.node_id == host.id
        assert alert.correlation == "ip"
        assert alert.severity is Severity.CRITICAL

    def test_internal_endpoint_is_preferred_in_either_direction(self, topology):
        host = next(n for n in topology.nodes if n.device_type is DeviceType.SERVER)
        correlator = Correlator.from_topology(topology)
        outbound = correlator.apply(Alert(src_ip=host.ip, dst_ip="203.0.113.1", score=0.8))
        inbound = correlator.apply(Alert(src_ip="203.0.113.1", dst_ip=host.ip, score=0.8))
        assert outbound.node_id == inbound.node_id == host.id

    def test_unknown_address_in_a_known_subnet_falls_back_to_the_gateway(self, topology):
        correlator = Correlator.from_topology(topology)
        subnet_prefix = topology.nodes[5].subnet.rsplit(".", 1)[0]
        alert = correlator.apply(Alert(src_ip=f"{subnet_prefix}.249", dst_ip="203.0.113.1", score=0.9))
        assert alert.node_id is not None
        assert alert.correlation == "subnet"

    def test_unresolvable_alerts_are_counted_not_dropped(self, topology):
        """An alert the map cannot place is an inventory gap. Hiding it hides
        the gap."""
        correlator = Correlator.from_topology(topology)
        alert = correlator.apply(Alert(src_ip="198.51.100.7", dst_ip="203.0.113.9", score=0.99))
        assert alert.node_id is None
        assert alert.correlation == "unresolved"
        assert correlator.unresolved_report()[0]["ip"] in {"198.51.100.7", "203.0.113.9"}
        assert correlator.stats()["unresolved_alerts"] == 1

    def test_mac_takes_precedence_over_ip(self, topology):
        host = next(n for n in topology.nodes if n.mac)
        correlator = Correlator.from_topology(topology)
        result = correlator.correlate(Alert(src_ip="198.51.100.1", dst_ip="203.0.113.1"), mac=host.mac)
        assert result.node_id == host.id and result.method == "mac"

    def test_reindex_after_topology_change(self, topology):
        correlator = Correlator.from_topology(topology)
        smaller = synthesize(40, seed=99)
        correlator.reindex(smaller)
        assert correlator.stats()["indexed_ips"] == len(
            {n.ip for n in smaller.nodes if n.ip})

    @pytest.mark.parametrize("ip,expected", [
        ("10.1.2.3", True), ("192.168.1.1", True), ("172.16.0.1", True),
        ("8.8.8.8", False), ("203.0.113.1", False), ("garbage", False),
    ])
    def test_internal_detection(self, ip, expected):
        assert is_internal(ip) is expected


class TestSynthesize:
    def test_shape_is_realistic(self):
        topology = synthesize(300, seed=7)
        stats = topology.stats()
        assert stats["nodes"] > 300
        assert stats["by_type"]["workstation"] > stats["by_type"]["router"]
        assert stats["by_type"]["router"] >= 2
        assert len(stats["by_subnet"]) >= 8

    def test_every_host_has_an_uplink(self):
        topology = synthesize(150, seed=8)
        connected = set()
        for link in topology.links:
            connected.add(link.source)
            connected.add(link.target)
        orphans = [n.id for n in topology.nodes if n.id not in connected]
        assert orphans == []

    def test_addresses_and_macs_are_unique(self):
        topology = synthesize(200, seed=9)
        macs = [n.mac for n in topology.nodes if n.mac]
        assert len(macs) == len(set(macs))
