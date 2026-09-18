"""Discovery parsing: the formats real networks actually emit."""
import pytest

from discovery.arp import parse_arp_output, parse_proc_net_arp
from discovery.lldp import parse_lldp_json, parse_lldp_text
from discovery.oui import is_locally_administered, normalize_mac, vendor_of
from discovery.snmp import (
    device_type_from, parse_snmp_device, parse_snmpwalk, vendor_from_object_id,
)


class TestMacNormalisation:
    @pytest.mark.parametrize("raw", [
        "00:1c:23:04:56:78", "0:1c:23:4:56:78", "00-1C-23-04-56-78",
        "001c.2304.5678", "001C23045678", "00 1c 23 04 56 78",
    ])
    def test_every_common_spelling_normalises_the_same(self, raw):
        assert normalize_mac(raw) == "00:1c:23:04:56:78"

    @pytest.mark.parametrize("raw", ["", None, "zz", "1.2.3.4", "00:1c:23:04:56"])
    def test_rejects_non_macs(self, raw):
        assert normalize_mac(raw) is None

    def test_locally_administered_detection(self):
        assert is_locally_administered("02:11:22:33:44:55")
        assert not is_locally_administered("00:1c:23:04:56:78")

    def test_vendor_lookup(self):
        assert vendor_of("00:0c:29:aa:bb:cc") == "VMware"
        assert vendor_of("b8:27:eb:11:22:33") == "RaspberryPi"
        assert vendor_of("ff:ff:ff:00:00:00") == "unknown"


class TestArp:
    def test_bsd_format(self):
        obs = parse_arp_output(
            "? (10.0.0.1) at 0:1c:23:4:56:78 on en0 ifscope [ethernet]\n"
            "gateway (10.0.0.254) at ab:cd:ef:12:34:56 on en0 ifscope [ethernet]"
        )
        assert [(o.ip, o.mac) for o in obs] == [
            ("10.0.0.1", "00:1c:23:04:56:78"),
            ("10.0.0.254", "ab:cd:ef:12:34:56"),
        ]
        assert obs[1].hostname == "gateway"

    def test_linux_arp_format(self):
        obs = parse_arp_output("gw (192.168.1.1) at 00:1b:54:11:22:33 [ether] on eth0")
        assert obs[0].ip == "192.168.1.1" and obs[0].interface == "eth0"

    def test_ip_neigh_format_including_ipv6(self):
        obs = parse_arp_output(
            "10.1.0.1 dev eth0 lladdr 00:1b:54:aa:bb:cc REACHABLE\n"
            "fe80::1 dev eth0 lladdr 00:99:9b:00:11:22 router REACHABLE"
        )
        assert len(obs) == 2
        assert obs[1].ip == "fe80::1"

    def test_incomplete_entries_are_skipped(self):
        """An unanswered ARP entry is not a device; inventing one fills the map
        with hosts that do not exist."""
        obs = parse_arp_output(
            "? (10.0.0.77) at (incomplete) on en0 [ethernet]\n"
            "? (10.0.0.78) at <incomplete> on en0\n"
            "10.0.0.79 dev eth0  FAILED\n"
            "? (10.0.0.80) at 00:1c:23:04:56:78 on en0"
        )
        assert [o.ip for o in obs] == ["10.0.0.80"]

    def test_proc_net_arp_skips_flag_zero(self):
        obs = parse_proc_net_arp(
            "IP address       HW type     Flags       HW address            Mask     Device\n"
            "192.168.0.10     0x1         0x2         00:18:8b:12:34:56     *        eth0\n"
            "192.168.0.11     0x1         0x0         00:00:00:00:00:00     *        eth0"
        )
        assert [o.ip for o in obs] == ["192.168.0.10"]

    def test_duplicate_lines_collapse(self):
        line = "? (10.0.0.1) at 00:1c:23:04:56:78 on en0"
        assert len(parse_arp_output(f"{line}\n{line}")) == 1

    def test_garbage_does_not_raise(self):
        assert parse_arp_output("total nonsense\n\n???") == []


class TestLldp:
    def _hosts_links(self, doc):
        return parse_lldp_json(doc, "edge-01")

    def test_lldpd_list_shape(self):
        hosts, links = self._hosts_links({"lldp": {"interface": [{
            "name": "eth0",
            "chassis": [{"name": [{"value": "core-sw-01"}],
                         "id": [{"type": "mac", "value": "00:1b:54:11:22:33"}],
                         "descr": [{"value": "Cisco IOS"}],
                         "capability": [{"type": "Bridge", "enabled": True}],
                         "mgmt-ip": [{"value": "10.0.0.2"}]}],
            "port": [{"id": [{"type": "ifname", "value": "Gi0/1"}]}],
        }]}})
        assert hosts[0].hostname == "core-sw-01"
        assert hosts[0].mac == "00:1b:54:11:22:33"
        assert hosts[0].device_type_hint == "switch"
        assert hosts[0].ip == "10.0.0.2"
        assert (links[0].local, links[0].remote, links[0].remote_port) == \
               ("edge-01", "core-sw-01", "Gi0/1")

    def test_lldpd_dict_shape(self):
        """lldpd emits a different JSON shape across versions; both must work."""
        hosts, links = self._hosts_links({"lldp": {"interface": {"eth1": {
            "chassis": {"dist-sw-02": {"id": {"type": "mac", "value": "00:99:9b:aa:bb:cc"},
                                       "capability": {"type": "Router", "enabled": True}}},
            "port": {"id": {"type": "ifname", "value": "xe-0/0/1"}},
        }}}})
        assert hosts[0].hostname == "dist-sw-02"
        assert hosts[0].device_type_hint == "router"
        assert links[0].remote == "dist-sw-02"

    def test_disabled_capabilities_are_ignored(self):
        hosts, _ = self._hosts_links({"lldp": {"interface": [{
            "name": "eth0",
            "chassis": [{"name": [{"value": "x"}],
                         "capability": [{"type": "Router", "enabled": False},
                                        {"type": "Bridge", "enabled": True}]}],
            "port": [{"id": [{"value": "1"}]}],
        }]}})
        assert hosts[0].device_type_hint == "switch"

    def test_text_format(self):
        hosts, links = parse_lldp_text(
            "-------------------------------------------\n"
            "Interface:    eth0, via: LLDP, RID: 1\n"
            "  Chassis:\n"
            "    ChassisID:    mac 00:1b:54:11:22:33\n"
            "    SysName:      core-sw-01\n"
            "    SysDescr:     Cisco IOS C2960X\n"
            "    Capability:   Bridge, on\n"
            "    MgmtIP:       10.0.0.2\n"
            "  Port:\n"
            "    PortID:       ifname Gi0/1\n"
            "Interface:    eth1, via: LLDP, RID: 2\n"
            "  Chassis:\n"
            "    ChassisID:    mac 6c:f3:7f:00:11:22\n"
            "    SysName:      ap-floor2\n"
            "    Capability:   Wlan, on\n"
            "  Port:\n"
            "    PortID:       ifname wlan0\n",
            local_name="edge-01",
        )
        assert [h.hostname for h in hosts] == ["core-sw-01", "ap-floor2"]
        assert hosts[1].device_type_hint == "wireless_ap"
        assert len(links) == 2

    def test_empty_input(self):
        assert parse_lldp_json({"lldp": {"interface": []}}) == ([], [])
        assert parse_lldp_text("") == ([], [])


class TestSnmp:
    WALK = (
        "SNMPv2-MIB::sysDescr.0 = STRING: Cisco IOS Software, C2960X Software\n"
        "SNMPv2-MIB::sysObjectID.0 = OID: SNMPv2-SMI::enterprises.9.1.1208\n"
        "SNMPv2-MIB::sysName.0 = STRING: core-sw-01\n"
        "SNMPv2-MIB::sysLocation.0 = STRING: DC1 Rack 4\n"
        "SNMPv2-MIB::sysServices.0 = INTEGER: 6\n"
        "IF-MIB::ifDescr.1 = STRING: GigabitEthernet0/1\n"
        "IF-MIB::ifPhysAddress.1 = STRING: 0:1b:54:11:22:33\n"
        "IP-MIB::ipNetToMediaPhysAddress.1.10.0.1.15 = STRING: 0:c:29:aa:bb:cc\n"
        "IP-MIB::ipNetToMediaPhysAddress.1.10.0.1.17 = STRING: 0:0:0:0:0:0\n"
        "LLDP-MIB::lldpRemSysName.0.1.1 = STRING: dist-rtr-01\n"
        "LLDP-MIB::lldpRemPortId.0.1.1 = STRING: xe-0/0/3\n"
        "SNMPv2-MIB::sysContact.0 = No Such Object available on this agent at this OID\n"
    )

    def test_device_identity(self):
        device, _, _ = parse_snmp_device(self.WALK, "10.0.0.2")
        assert device.hostname == "core-sw-01"
        assert device.mac == "00:1b:54:11:22:33"
        assert device.device_type_hint == "switch"
        assert device.vendor_hint == "Cisco"
        assert device.extra["location"] == "DC1 Rack 4"

    def test_arp_table_yields_neighbours(self):
        """One walk of a router maps every host in its subnets."""
        _, neighbours, _ = parse_snmp_device(self.WALK, "10.0.0.2")
        assert [(n.ip, n.mac) for n in neighbours] == [("10.0.1.15", "00:0c:29:aa:bb:cc")]

    def test_lldp_remote_table_yields_links(self):
        _, _, links = parse_snmp_device(self.WALK, "10.0.0.2")
        assert (links[0].local, links[0].remote, links[0].remote_port) == \
               ("core-sw-01", "dist-rtr-01", "xe-0/0/3")

    def test_numeric_oids(self):
        device, _, _ = parse_snmp_device(
            ".1.3.6.1.2.1.1.5.0 = STRING: edge-fw-01\n"
            ".1.3.6.1.2.1.1.1.0 = STRING: FortiGate-60F v7.0.5\n"
            ".1.3.6.1.2.1.1.2.0 = OID: .1.3.6.1.4.1.12356.101.1.600\n", "10.0.0.1")
        assert device.hostname == "edge-fw-01"
        assert device.device_type_hint == "firewall"
        assert device.vendor_hint == "Fortinet"

    def test_no_such_object_lines_are_dropped(self):
        walk = parse_snmpwalk(self.WALK)
        assert "sysContact" not in walk

    @pytest.mark.parametrize("descr,services,expected", [
        ("HP LaserJet 400", None, "printer"),
        ("Juniper mx204 JUNOS 21.2", "4", "router"),
        ("Linux db01 5.15.0", "72", "server"),
        ("Cisco Catalyst 9300", "6", "switch"),
        ("", "2", "switch"),
        ("", "4", "router"),
        ("", "72", "server"),
        ("", None, "unknown"),
    ])
    def test_classification(self, descr, services, expected):
        assert device_type_from(descr, services) == expected

    def test_vendor_from_both_oid_spellings(self):
        assert vendor_from_object_id(".1.3.6.1.4.1.9.1.1208") == "Cisco"
        assert vendor_from_object_id("SNMPv2-SMI::enterprises.2636.1.1") == "Juniper"
        assert vendor_from_object_id("") == ""
