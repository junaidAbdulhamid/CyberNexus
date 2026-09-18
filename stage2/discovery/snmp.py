"""SNMP response parsing.

SNMP is the highest-yield active source.  A single walk of one core switch
returns the device's own identity, every interface, *and* ``ipNetToMedia`` — the
router's complete ARP table, which is every IP-to-MAC binding in the subnets it
serves.  One query maps a whole floor.

This module parses ``snmpwalk`` output rather than speaking SNMP itself, which
keeps it dependency-free and testable against captured output.  ``probe.py``
adds live querying via pysnmp when it is installed.

Both OID spellings are handled: symbolic (``SNMPv2-MIB::sysName.0``) and numeric
(``.1.3.6.1.2.1.1.5.0``), because whether you get names depends on which MIBs
happen to be installed on the box running the walk.
"""
from __future__ import annotations

import re
from typing import Iterable

from .observations import HostObservation, LinkObservation
from .oui import normalize_mac

SOURCE = "snmp"

# "SNMPv2-MIB::sysName.0 = STRING: core-sw-01"
_LINE = re.compile(r"^(?P<oid>[^\s=]+)\s*=\s*(?:(?P<type>[A-Za-z0-9-]+):\s*)?(?P<value>.*)$")

NUMERIC_NAMES = {
    "1.3.6.1.2.1.1.1": "sysDescr",
    "1.3.6.1.2.1.1.2": "sysObjectID",
    "1.3.6.1.2.1.1.3": "sysUpTime",
    "1.3.6.1.2.1.1.4": "sysContact",
    "1.3.6.1.2.1.1.5": "sysName",
    "1.3.6.1.2.1.1.6": "sysLocation",
    "1.3.6.1.2.1.1.7": "sysServices",
    "1.3.6.1.2.1.2.2.1.2": "ifDescr",
    "1.3.6.1.2.1.2.2.1.5": "ifSpeed",
    "1.3.6.1.2.1.2.2.1.6": "ifPhysAddress",
    "1.3.6.1.2.1.2.2.1.8": "ifOperStatus",
    "1.3.6.1.2.1.31.1.1.1.1": "ifName",
    "1.3.6.1.2.1.4.22.1.2": "ipNetToMediaPhysAddress",
    "1.0.8802.1.1.2.1.4.1.1.9": "lldpRemSysName",
    "1.0.8802.1.1.2.1.4.1.1.7": "lldpRemPortId",
    "1.0.8802.1.1.2.1.4.1.1.10": "lldpRemSysDesc",
}

#: sysObjectID enterprise number -> vendor.  Cheap, and it works even when the
#: device's MAC is hidden behind a router.
ENTERPRISE_VENDOR = {
    "9": "Cisco", "2636": "Juniper", "11": "HewlettPackard", "674": "Dell",
    "1916": "Extreme", "12356": "Fortinet", "14823": "Aruba", "4526": "Netgear",
    "8072": "NetSNMP", "2011": "Huawei", "25506": "H3C", "6876": "VMware",
    "311": "Microsoft", "42": "Oracle",
}

#: Keywords in sysDescr -> device type.  Order matters: "router" before "linux",
#: because a Linux-based router should map to router.
DESCR_TYPE = (
    ("firewall", "firewall"), ("fortigate", "firewall"), ("palo alto", "firewall"),
    ("asa", "firewall"), ("access point", "wireless_ap"), ("wireless", "wireless_ap"),
    # Switch model families are checked before the generic "router" keyword: a
    # Catalyst that also routes still belongs on the map as a switch.
    ("catalyst", "switch"), ("procurve", "switch"), ("nexus", "switch"),
    ("c2960", "switch"), ("c3560", "switch"), ("c3750", "switch"), ("c9300", "switch"),
    ("ex2200", "switch"), ("ex4300", "switch"), ("aruba 25", "switch"),
    ("switch", "switch"),
    ("router", "router"), ("ios-xr", "router"), ("junos", "router"), ("isr", "router"),
    ("printer", "printer"), ("laserjet", "printer"), ("jetdirect", "printer"),
    ("windows", "workstation"), ("macos", "workstation"), ("darwin", "workstation"),
    ("linux", "server"), ("ubuntu", "server"), ("centos", "server"), ("esxi", "server"),
)


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _split_oid(oid: str) -> tuple[str, str]:
    """Return ``(name, index)`` for symbolic or numeric OIDs."""
    oid = oid.strip().lstrip(".")
    if "::" in oid:
        _, _, rest = oid.partition("::")
        name, _, index = rest.partition(".")
        return name, index
    # Numeric: find the longest known prefix.
    for prefix, name in sorted(NUMERIC_NAMES.items(), key=lambda kv: -len(kv[0])):
        if oid == prefix:
            return name, ""
        if oid.startswith(prefix + "."):
            return name, oid[len(prefix) + 1:]
    return oid, ""


def parse_snmpwalk(text: str) -> dict[str, dict[str, str]]:
    """``{varbind_name: {index: value}}`` from raw snmpwalk output."""
    out: dict[str, dict[str, str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        m = _LINE.match(line)
        if not m:
            continue
        value = _strip_quotes(m.group("value"))
        if value in {"No Such Object available on this agent at this OID",
                     "No Such Instance currently exists at this OID", ""}:
            continue
        name, index = _split_oid(m.group("oid"))
        out.setdefault(name, {})[index] = value
    return out


def device_type_from(sys_descr: str, sys_services: str | None, sys_object_id: str = "") -> str:
    """Classify a device from what SNMP tells us about it.

    sysServices is a bitmask (bit 2 = internet/routing, bit 4 = end-to-end,
    bit 7 = applications) and is a weaker signal than sysDescr, so the
    description is tried first and the bitmask only breaks ties.
    """
    descr = (sys_descr or "").lower()
    for needle, device_type in DESCR_TYPE:
        if needle in descr:
            return device_type
    try:
        services = int(sys_services) if sys_services else 0
    except ValueError:
        services = 0
    # Bit 2 (0x02) is datalink forwarding - bridging.  A pure router does not
    # bridge, so this bit is the stronger switch signal even when bit 3 (0x04,
    # internet/routing) is also set, which every L3 switch reports.
    if services & 0x02:
        return "switch"
    if services & 0x04:
        return "router"
    if services & 0x40:
        return "server"
    return "unknown"


def vendor_from_object_id(sys_object_id: str) -> str:
    """``.1.3.6.1.4.1.9.1.1208`` -> Cisco (enterprise 9)."""
    if not sys_object_id:
        return ""
    # Numeric (.1.3.6.1.4.1.9.1.1208) or symbolic (enterprises.9.1.1208).
    m = re.search(r"1\.3\.6\.1\.4\.1\.(\d+)", sys_object_id) or re.search(
        r"enterprises[.:](\d+)", sys_object_id
    )
    if not m:
        return ""
    return ENTERPRISE_VENDOR.get(m.group(1), "")


def _mac_from_snmp(value: str) -> str | None:
    """SNMP renders MACs as ``0:1b:54:11:22:33`` or ``00 1B 54 11 22 33``."""
    return normalize_mac(value.replace(" ", ":").strip())


def parse_snmp_device(
    text: str, host_ip: str | None = None
) -> tuple[HostObservation, list[HostObservation], list[LinkObservation]]:
    """Parse one device's walk.

    Returns the device itself, any hosts learned from its ARP table, and any
    links learned from its LLDP remote table.
    """
    walk = parse_snmpwalk(text)

    def first(name: str, default: str = "") -> str:
        values = walk.get(name) or {}
        return next(iter(values.values()), default)

    sys_name = first("sysName")
    sys_descr = first("sysDescr")
    sys_object_id = first("sysObjectID")
    sys_services = first("sysServices")
    sys_location = first("sysLocation")

    phys = walk.get("ifPhysAddress", {})
    device_mac = None
    for value in phys.values():
        candidate = _mac_from_snmp(value)
        if candidate and candidate != "00:00:00:00:00:00":
            device_mac = candidate
            break

    interfaces = []
    for index, name in sorted((walk.get("ifName") or walk.get("ifDescr") or {}).items()):
        interfaces.append(name)

    device = HostObservation(
        ip=host_ip, mac=device_mac, hostname=sys_name, source=SOURCE,
        device_type_hint=device_type_from(sys_descr, sys_services, sys_object_id),
        vendor_hint=vendor_from_object_id(sys_object_id), os_hint=sys_descr,
        extra={"location": sys_location, "interfaces": interfaces,
               "sys_object_id": sys_object_id},
    )

    # ipNetToMediaPhysAddress index is "<ifIndex>.<a>.<b>.<c>.<d>"
    neighbours: list[HostObservation] = []
    for index, value in (walk.get("ipNetToMediaPhysAddress") or {}).items():
        parts = index.split(".")
        if len(parts) < 5:
            continue
        ip = ".".join(parts[-4:])
        mac = _mac_from_snmp(value)
        if not mac or mac == "00:00:00:00:00:00":
            continue
        neighbours.append(HostObservation(
            ip=ip, mac=mac, source=f"{SOURCE}:arp",
            extra={"learned_from": sys_name or host_ip or ""},
        ))

    links: list[LinkObservation] = []
    rem_names = walk.get("lldpRemSysName") or {}
    rem_ports = walk.get("lldpRemPortId") or {}
    local_ports = walk.get("ifName") or walk.get("ifDescr") or {}
    for index, remote_name in rem_names.items():
        # lldpRemSysName index: <timeMark>.<localPortNum>.<remoteIndex>
        parts = index.split(".")
        local_port_num = parts[1] if len(parts) >= 2 else ""
        links.append(LinkObservation(
            local=sys_name or host_ip or "",
            remote=remote_name,
            local_port=local_ports.get(local_port_num, local_port_num),
            remote_port=rem_ports.get(index, ""),
            source=SOURCE,
        ))
    return device, neighbours, links


def parse_snmp_devices(walks: Iterable[tuple[str, str]]):
    """Parse several ``(host_ip, walk_text)`` pairs."""
    devices, neighbours, links = [], [], []
    for host_ip, text in walks:
        device, hosts, device_links = parse_snmp_device(text, host_ip)
        devices.append(device)
        neighbours.extend(hosts)
        links.extend(device_links)
    return devices, neighbours, links
