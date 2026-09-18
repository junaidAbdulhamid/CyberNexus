"""MAC address -> vendor.

Ships a small built-in table of the OUIs that actually show up in enterprise
networks, because the full IEEE registry is ~3 MB and this is a demo.
``load_oui_file`` takes the real thing (IEEE ``oui.csv`` or a Wireshark ``manuf``
file) when accuracy matters.

Vendor matters to the map for a practical reason: an operator filtering "show
me the Cisco gear" is filtering by the thing they can physically walk up to.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

#: prefix (first 3 octets, uppercase hex, no separators) -> vendor
BUILTIN_OUI: dict[str, str] = {
    "00000C": "Cisco", "001B54": "Cisco", "0023AB": "Cisco", "E0D173": "Cisco",
    "00E08F": "Cisco", "5057A8": "Cisco",
    "001018": "Broadcom", "000AF7": "Broadcom",
    "000B86": "Aruba", "6CF37F": "Aruba", "94B40F": "Aruba",
    "000C29": "VMware", "005056": "VMware", "001C14": "VMware",
    "0050F2": "Microsoft", "000D3A": "Microsoft", "7C1E52": "Microsoft",
    "001A11": "Google", "3C5AB4": "Google", "F4F5E8": "Google",
    "000393": "Apple", "001451": "Apple", "3C0754": "Apple", "A85C2C": "Apple",
    "F0189E": "Apple", "8C8590": "Apple",
    "001C23": "Dell", "00188B": "Dell", "B083FE": "Dell", "F8BC12": "Dell",
    "001321": "HewlettPackard", "001F29": "HewlettPackard", "3C4A92": "HewlettPackard",
    "0017A4": "HewlettPackard", "94577A": "HewlettPackard",
    "001B21": "Intel", "00A0C9": "Intel", "3CFDFE": "Intel", "A0369F": "Intel",
    "001D0F": "TPLink", "5C63BF": "TPLink",
    "0004F2": "Polycom", "00907F": "WatchGuard",
    "00095B": "Netgear", "20E52A": "Netgear",
    "000F4B": "Oracle", "0021F6": "Oracle",
    "001E8F": "Canon", "0000AA": "Xerox", "00804C": "Brother",
    "000E8F": "Sercomm", "B827EB": "RaspberryPi", "DCA632": "RaspberryPi",
    "001788": "Philips", "18B430": "Nest", "A4CF12": "Espressif", "2462AB": "Espressif",
    "000569": "VMware", "0025B5": "Cisco", "00155D": "Microsoft",
    "001759": "Fortinet", "090F00": "Fortinet", "704CA5": "Fortinet",
    "00099B": "Juniper", "2C6BF5": "Juniper", "3C8AB0": "Juniper",
    "0004E2": "SMC", "000BDB": "Dell", "001AA0": "Dell",
    "6805CA": "Intel", "94C691": "Ubiquiti", "788A20": "Ubiquiti", "FCECDA": "Ubiquiti",
    "00265A": "DLink", "1CBDB9": "DLink",
    "001C7F": "CheckPoint", "00E02B": "Extreme", "00049F": "Freescale",
}

_MAC_CLEAN = re.compile(r"[^0-9A-Fa-f]")
_MAC_VALID = re.compile(r"^[0-9A-Fa-f]{12}$")

_registry: dict[str, str] = dict(BUILTIN_OUI)


def normalize_mac(mac: str | None) -> str | None:
    """Canonical lowercase colon form, or None if it is not a MAC.

    Discovery sources disagree on formatting (``0:1c:23:4:56:78`` from BSD arp,
    ``00-1C-23-04-56-78`` from Windows, ``001c.2304.5678`` from Cisco), and a
    node keyed on an unnormalised MAC duplicates itself the moment a second
    source sees it.
    """
    if not mac:
        return None
    cleaned = _MAC_CLEAN.sub("", mac)
    if len(cleaned) != 12:
        # BSD `arp` prints short octets: 0:1c:23:4:56:78
        parts = re.split(r"[:\-.]", mac.strip())
        if len(parts) == 6 and all(p and len(p) <= 2 for p in parts):
            cleaned = "".join(p.rjust(2, "0") for p in parts)
        else:
            return None
    if not _MAC_VALID.match(cleaned):
        return None
    lower = cleaned.lower()
    return ":".join(lower[i:i + 2] for i in range(0, 12, 2))


def oui_of(mac: str | None) -> str | None:
    norm = normalize_mac(mac)
    if not norm:
        return None
    return norm.replace(":", "")[:6].upper()


def vendor_of(mac: str | None, default: str = "unknown") -> str:
    prefix = oui_of(mac)
    if not prefix:
        return default
    return _registry.get(prefix, default)


def is_locally_administered(mac: str | None) -> bool:
    """True for randomised / virtual MACs (bit 1 of the first octet).

    These are worth flagging: a locally administered address means the vendor
    lookup is meaningless, and modern clients randomise per network.
    """
    norm = normalize_mac(mac)
    if not norm:
        return False
    return bool(int(norm[:2], 16) & 0x02)


def load_oui_file(path: str | Path) -> int:
    """Merge an IEEE ``oui.csv`` or Wireshark ``manuf`` file into the registry."""
    path = Path(path)
    added = 0
    text = path.read_text(errors="replace")
    if path.suffix.lower() == ".csv":
        for row in csv.DictReader(text.splitlines()):
            prefix = (row.get("Assignment") or "").strip().upper()
            name = (row.get("Organization Name") or "").strip()
            if len(prefix) == 6 and name:
                _registry[prefix] = name
                added += 1
    else:  # wireshark manuf format: "00:00:0C  Cisco  Cisco Systems, Inc"
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 2)
            if len(parts) < 2:
                continue
            prefix = _MAC_CLEAN.sub("", parts[0].split("/")[0]).upper()
            if len(prefix) >= 6:
                _registry[prefix[:6]] = parts[1]
                added += 1
    return added


def registry_size() -> int:
    return len(_registry)
