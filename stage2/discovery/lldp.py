"""LLDP neighbour parsing.

LLDP is the only source that reports *physical adjacency* — which switch port a
device is actually plugged into.  ARP tells you a host exists somewhere in a
broadcast domain; LLDP tells you where the cable goes, which is what makes the
map's link layer real rather than inferred.

Two input formats:

* ``lldpcli -f json show neighbors`` — preferred, but the JSON shape varies
  between lldpd versions (``interface`` is sometimes a list, sometimes an object
  keyed by name; leaf values are sometimes ``{"value": x}``, sometimes ``x``),
  so the walker below is deliberately tolerant.
* ``lldpctl`` / ``lldpcli show neighbors`` plain text.
"""
from __future__ import annotations

import json
import subprocess
from typing import Any

from .observations import HostObservation, LinkObservation
from .oui import normalize_mac

SOURCE = "lldp"

#: LLDP capability -> our device type, most specific first.
CAPABILITY_TYPE = (
    ("router", "router"),
    ("wlan", "wireless_ap"),
    ("wlan access point", "wireless_ap"),
    ("bridge", "switch"),
    ("telephone", "iot"),
    ("station", "workstation"),
    ("repeater", "switch"),
)


def _unwrap(value: Any) -> Any:
    """lldpd wraps leaves as ``{"value": x}`` and repeats as ``[{...}]``."""
    if isinstance(value, list):
        return _unwrap(value[0]) if value else None
    if isinstance(value, dict):
        if "value" in value:
            return value["value"]
        return value
    return value


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _capabilities(chassis: dict) -> list[str]:
    caps = []
    for cap in _as_list(chassis.get("capability")):
        if isinstance(cap, dict):
            enabled = cap.get("enabled", True)
            name = cap.get("type") or cap.get("value")
            if name and enabled:
                caps.append(str(name).lower())
        elif cap:
            caps.append(str(cap).lower())
    return caps


def _device_type(capabilities: list[str]) -> str:
    joined = " ".join(capabilities)
    for needle, device_type in CAPABILITY_TYPE:
        if needle in joined:
            return device_type
    return ""


def parse_lldp_json(text: str | dict, local_name: str = "localhost") -> tuple[list[HostObservation], list[LinkObservation]]:
    data = json.loads(text) if isinstance(text, str) else text
    lldp = data.get("lldp", data)
    interfaces = lldp.get("interface", lldp.get("interfaces", []))

    # `interface` may be a list of {name: ...}, a list of {"eth0": {...}}, or a
    # dict keyed by interface name.  Normalise all three to (name, body) pairs.
    entries: list[tuple[str, dict]] = []
    if isinstance(interfaces, dict):
        for name, body in interfaces.items():
            for item in _as_list(body):
                entries.append((name, item))
    else:
        for item in _as_list(interfaces):
            if not isinstance(item, dict):
                continue
            if "name" in item:
                entries.append((str(_unwrap(item.get("name")) or ""), item))
            else:
                for name, body in item.items():
                    for sub in _as_list(body):
                        entries.append((name, sub))

    hosts: list[HostObservation] = []
    links: list[LinkObservation] = []
    for iface_name, body in entries:
        if not isinstance(body, dict):
            continue
        chassis_raw = body.get("chassis")
        chassis: dict = {}
        if isinstance(chassis_raw, dict) and "name" not in chassis_raw and "id" not in chassis_raw:
            # {"core-sw-01": {...}} form: the key is the system name
            for name, sub in chassis_raw.items():
                sub = _unwrap(sub)
                if isinstance(sub, dict):
                    chassis = dict(sub)
                    chassis.setdefault("name", name)
                break
        else:
            chassis = _unwrap(chassis_raw) or {}
        if not isinstance(chassis, dict):
            continue

        sys_name = str(_unwrap(chassis.get("name")) or "")
        descr = str(_unwrap(chassis.get("descr")) or "")
        chassis_id = _unwrap(chassis.get("id"))
        mac = None
        if isinstance(chassis_id, dict):
            if str(chassis_id.get("type", "")).lower() == "mac":
                mac = normalize_mac(str(chassis_id.get("value", "")))
        elif chassis_id:
            mac = normalize_mac(str(chassis_id))

        mgmt_ip = None
        for key in ("mgmt-ip", "mgmt_ip", "mgmt-ip-v4"):
            value = _unwrap(chassis.get(key))
            if value:
                mgmt_ip = str(value)
                break

        port = _unwrap(body.get("port")) or {}
        port_id = _unwrap(port.get("id")) if isinstance(port, dict) else None
        if isinstance(port_id, dict):
            port_id = port_id.get("value")
        port_descr = _unwrap(port.get("descr")) if isinstance(port, dict) else None

        caps = _capabilities(chassis)
        hosts.append(HostObservation(
            ip=mgmt_ip, mac=mac, hostname=sys_name, interface=iface_name, source=SOURCE,
            device_type_hint=_device_type(caps), os_hint=descr,
            extra={"capabilities": caps, "port_descr": port_descr or ""},
        ))
        if sys_name or mac:
            links.append(LinkObservation(
                local=local_name, remote=sys_name or mac or "",
                local_port=iface_name, remote_port=str(port_id or port_descr or ""),
                source=SOURCE, remote_mac=mac,
            ))
    return hosts, links


def parse_lldp_text(text: str, local_name: str = "localhost") -> tuple[list[HostObservation], list[LinkObservation]]:
    """Parse ``lldpctl`` plain-text output."""
    hosts: list[HostObservation] = []
    links: list[LinkObservation] = []
    current: dict | None = None

    def flush() -> None:
        if not current or not (current.get("sys_name") or current.get("mac")):
            return
        caps = current.get("caps", [])
        hosts.append(HostObservation(
            ip=current.get("mgmt_ip"), mac=current.get("mac"),
            hostname=current.get("sys_name", ""), interface=current.get("iface", ""),
            source=SOURCE, device_type_hint=_device_type(caps),
            os_hint=current.get("descr", ""),
            extra={"capabilities": caps, "port_descr": current.get("port_descr", "")},
        ))
        links.append(LinkObservation(
            local=local_name, remote=current.get("sys_name") or current.get("mac") or "",
            local_port=current.get("iface", ""),
            remote_port=current.get("port_id") or current.get("port_descr", ""),
            source=SOURCE, remote_mac=current.get("mac"),
        ))

    for raw in text.splitlines():
        line = raw.strip()
        if not line or set(line) <= {"-"}:
            continue
        if line.startswith("Interface:"):
            flush()
            iface = line.split(":", 1)[1].split(",")[0].strip()
            current = {"iface": iface, "caps": []}
            continue
        if current is None:
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "chassisid":
            parts = value.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "mac":
                current["mac"] = normalize_mac(parts[1])
            else:
                current["mac"] = normalize_mac(value)
        elif key == "sysname":
            current["sys_name"] = value
        elif key == "sysdescr":
            current["descr"] = value
        elif key == "capability":
            name = value.split(",")[0].strip().lower()
            enabled = "off" not in value.lower()
            if name and enabled:
                current.setdefault("caps", []).append(name)
        elif key == "portid":
            parts = value.split(None, 1)
            current["port_id"] = parts[1] if len(parts) == 2 else value
        elif key == "portdescr":
            current["port_descr"] = value
        elif key in {"mgmtip", "mgmt-ip"}:
            current.setdefault("mgmt_ip", value)
    flush()
    return hosts, links


def read_local_lldp(timeout: float = 10.0, local_name: str = "localhost"):
    """Query the local lldpd, JSON first, falling back to text."""
    for cmd, parser in (
        (["lldpcli", "-f", "json", "show", "neighbors"], parse_lldp_json),
        (["lldpctl", "-f", "json"], parse_lldp_json),
        (["lldpcli", "show", "neighbors"], parse_lldp_text),
        (["lldpctl"], parse_lldp_text),
    ):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                return parser(proc.stdout, local_name)
            except (ValueError, KeyError, TypeError):
                continue
    return [], []
