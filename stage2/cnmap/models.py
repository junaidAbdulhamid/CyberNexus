"""Core domain model: devices, links, topology, alerts and node state.

Pydantic models rather than plain dataclasses because these objects are the
API contract — the same definitions generate the OpenAPI schema the frontend
is written against, so a field rename cannot silently diverge between the two.

One rule runs through the whole model, and it is a privacy rule as much as a
design one: **nothing here carries packet payloads**.  A node is addressing and
inventory metadata; an alert is a verdict plus the five-tuple that produced it.
Stage 1 deliberately never puts payload bytes on the wire, and Stage 2 never
asks for them.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class DeviceType(str, Enum):
    ROUTER = "router"
    SWITCH = "switch"
    FIREWALL = "firewall"
    SERVER = "server"
    WORKSTATION = "workstation"
    PRINTER = "printer"
    IOT = "iot"
    WIRELESS_AP = "wireless_ap"
    UNKNOWN = "unknown"


class Severity(str, Enum):
    """Five levels, ordered.  The ordering matters: the UI renders severity as
    a visual channel and the MTTI harness measures how fast the top level is
    found, so the scale has to be total and stable."""

    NONE = "none"
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self.value]

    @classmethod
    def from_score(cls, score: float) -> "Severity":
        """Map a Stage 1 attack probability onto the severity scale.

        The cut points are policy, not physics: Stage 1's calibrated threshold
        is ~0.22, so anything below that is not an alert at all, and the bands
        above it widen as confidence rises.
        """
        if score >= 0.95:
            return cls.CRITICAL
        if score >= 0.80:
            return cls.HIGH
        if score >= 0.55:
            return cls.MEDIUM
        if score >= 0.30:
            return cls.LOW
        return cls.INFO


_SEVERITY_RANK = {
    "none": 0, "info": 1, "low": 2, "medium": 3, "high": 4, "critical": 5,
}
SEVERITY_ORDER = tuple(Severity(v) for v in _SEVERITY_RANK)


class Vec3(BaseModel):
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


class Node(BaseModel):
    """A discovered device."""

    id: str = Field(description="Stable identifier, derived from MAC or IP")
    name: str = ""
    ip: Optional[str] = None
    ips: list[str] = Field(default_factory=list, description="All known addresses")
    mac: Optional[str] = None
    vendor: str = "unknown"
    device_type: DeviceType = DeviceType.UNKNOWN
    os: str = ""
    subnet: str = ""
    site: str = "default"
    # Business criticality, 0 (lab) to 3 (crown jewels).  Used to break ties in
    # the UI when several nodes alert at once.
    criticality: int = 1
    tags: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    discovered_by: list[str] = Field(default_factory=list)
    first_seen: float = Field(default_factory=time.time)
    last_seen: float = Field(default_factory=time.time)
    # Layout position, computed server-side so every client agrees on where a
    # host is - an operator who learns the map keeps that knowledge.
    position: Vec3 = Field(default_factory=Vec3)
    layer: int = 0

    @property
    def label(self) -> str:
        return self.name or self.ip or self.id


class Link(BaseModel):
    id: str
    source: str
    target: str
    kind: Literal["ethernet", "uplink", "wireless", "virtual", "inferred"] = "ethernet"
    bandwidth_mbps: Optional[int] = None
    discovered_by: list[str] = Field(default_factory=list)


class Topology(BaseModel):
    nodes: list[Node] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    version: int = 0
    generated_at: float = Field(default_factory=time.time)
    source: str = ""

    def node_index(self) -> dict[str, Node]:
        return {n.id: n for n in self.nodes}

    def stats(self) -> dict:
        by_type: dict[str, int] = {}
        by_subnet: dict[str, int] = {}
        for n in self.nodes:
            by_type[n.device_type.value] = by_type.get(n.device_type.value, 0) + 1
            by_subnet[n.subnet] = by_subnet.get(n.subnet, 0) + 1
        return {
            "nodes": len(self.nodes),
            "links": len(self.links),
            "by_type": by_type,
            "by_subnet": by_subnet,
            "vendors": sorted({n.vendor for n in self.nodes}),
        }


class Alert(BaseModel):
    """A Stage 1 verdict, as it reaches the map.

    Field names match Stage 1's alert payload so the connector is a pass-through
    plus correlation; ``node_id`` and ``severity`` are the only things Stage 2
    adds.
    """

    flow_id: str = ""
    src_ip: str = ""
    dst_ip: str = ""
    src_port: int = 0
    dst_port: int = 0
    protocol: int = 6
    score: float = 0.0
    threshold: float = 0.0
    detected_at: float = Field(default_factory=time.time)
    latency_s: float = 0.0
    label: float = -1.0
    # --- added by Stage 2 -------------------------------------------------
    node_id: Optional[str] = None
    peer_node_id: Optional[str] = None
    severity: Severity = Severity.INFO
    correlation: str = "none"
    received_at: float = Field(default_factory=time.time)

    @property
    def summary(self) -> str:
        return (f"{self.src_ip}:{self.src_port} -> {self.dst_ip}:{self.dst_port} "
                f"score {self.score:.3f}")


class NodeState(BaseModel):
    """Live per-node alert state, the thing the UI actually renders."""

    node_id: str
    severity: Severity = Severity.NONE
    score: float = 0.0
    alert_count: int = 0
    alert_count_window: int = 0
    first_alert_at: Optional[float] = None
    last_alert_at: Optional[float] = None
    last_alert_id: str = ""
    top_peers: list[str] = Field(default_factory=list)
    top_ports: list[int] = Field(default_factory=list)
    acknowledged: bool = False
    updated_at: float = Field(default_factory=time.time)


class NodeStateUpdate(BaseModel):
    """What goes over the WebSocket when a node changes."""

    type: Literal["node_state"] = "node_state"
    state: NodeState
    alert: Optional[Alert] = None
    seq: int = 0
    server_ts: float = Field(default_factory=time.time)


class TimelineEvent(BaseModel):
    """One entry in the history scrubber."""

    ts: float
    node_id: str
    severity: Severity
    score: float
    flow_id: str = ""
    src_ip: str = ""
    dst_ip: str = ""
    dst_port: int = 0


__all__ = [
    "Alert", "DeviceType", "Link", "Node", "NodeState", "NodeStateUpdate",
    "Severity", "SEVERITY_ORDER", "TimelineEvent", "Topology", "Vec3",
]
