"""Deterministic 3D layout.

The layout is computed on the server, once, and shipped with the topology.  That
is a deliberate choice: a force-directed graph that settles differently on every
page load destroys the one thing that makes a map fast to read — an operator
who has learned that "finance is the cluster on the left" keeps that knowledge
between shifts, and across two operators looking at two screens.

Shape: a layered city.

* subnets become **districts** laid out on a ring in the XZ plane, sized by host
  count, so each district has a fixed place;
* hosts fill their district on a phyllotaxis disc — even spacing, no lattice
  artefacts, and adding a host does not move the others much;
* infrastructure rises above the district it serves (access switches low, core
  routers at the top), so the spine of the network is legible from any angle and
  "up" consistently means "closer to the core".

Everything is seeded from the node id, so the same topology always produces the
same coordinates.
"""
from __future__ import annotations

import hashlib
import math
from typing import Iterable

from .models import DeviceType, Link, Node, Topology, Vec3

#: Height of each layer.  Gaps are generous because depth perception in a 3D
#: scene is weak; layers that are close together read as one plane.
LAYER_HEIGHT = {3: 46.0, 2: 30.0, 1: 15.0, 0: 0.0}

LAYER_OF_TYPE = {
    DeviceType.ROUTER: 3,
    DeviceType.FIREWALL: 3,
    DeviceType.SWITCH: 1,
    DeviceType.WIRELESS_AP: 1,
    DeviceType.SERVER: 0,
    DeviceType.WORKSTATION: 0,
    DeviceType.PRINTER: 0,
    DeviceType.IOT: 0,
    DeviceType.UNKNOWN: 0,
}

GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))


def _stable_unit(value: str) -> float:
    """A deterministic float in [0, 1) from a string."""
    digest = hashlib.blake2b(value.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / float(1 << 64)


def layer_of(node: Node) -> int:
    """Which tier a device belongs to.

    Distribution (layer 2) is not a device type, it is a role: a switch that
    other switches connect to.  ``assign_layers`` promotes those after the graph
    is known; this function only provides the default from the device type.
    """
    return LAYER_OF_TYPE.get(node.device_type, 0)


def assign_layers(nodes: list[Node], links: list[Link]) -> None:
    """Set ``node.layer``, promoting switches that aggregate other switches."""
    index = {n.id: n for n in nodes}
    for node in nodes:
        node.layer = layer_of(node)

    infra_types = {DeviceType.SWITCH, DeviceType.WIRELESS_AP}
    neighbours: dict[str, set[str]] = {n.id: set() for n in nodes}
    for link in links:
        if link.source in neighbours and link.target in neighbours:
            neighbours[link.source].add(link.target)
            neighbours[link.target].add(link.source)

    for node in nodes:
        if node.device_type not in infra_types:
            continue
        # A switch with switch/AP neighbours of its own is aggregating them.
        downstream = sum(
            1 for peer in neighbours[node.id]
            if index[peer].device_type in infra_types
        )
        if downstream >= 2:
            node.layer = 2


def _district_centers(subnets: list[tuple[str, int]], spread: float) -> dict[str, tuple[float, float]]:
    """Place subnet districts on a ring, ordered so the layout is stable.

    Districts are sorted by name rather than by size: sorting by size means a
    subnet that gains a host can jump across the map, which is exactly the kind
    of instability this layout exists to avoid.
    """
    centers: dict[str, tuple[float, float]] = {}
    count = max(len(subnets), 1)
    if count == 1:
        return {subnets[0][0]: (0.0, 0.0)} if subnets else {}
    total_hosts = sum(n for _, n in subnets) or 1
    # Ring radius grows with the square root of total population so density
    # stays roughly constant as the network grows.
    radius = spread * math.sqrt(total_hosts) / 2.0
    for i, (name, _size) in enumerate(subnets):
        angle = 2 * math.pi * i / count
        centers[name] = (radius * math.cos(angle), radius * math.sin(angle))
    return centers


def _disc_position(index: int, count: int, radius: float) -> tuple[float, float]:
    """Phyllotaxis: even coverage of a disc with no rings or rows."""
    if count <= 1:
        return 0.0, 0.0
    r = radius * math.sqrt((index + 0.5) / count)
    theta = GOLDEN_ANGLE * index
    return r * math.cos(theta), r * math.sin(theta)


def compute_layout(topology: Topology, spread: float = 9.0, jitter: float = 0.35) -> Topology:
    """Assign a position to every node.  Idempotent and deterministic."""
    nodes, links = topology.nodes, topology.links
    assign_layers(nodes, links)

    hosts_by_subnet: dict[str, list[Node]] = {}
    infra: list[Node] = []
    for node in nodes:
        if node.layer == 0:
            hosts_by_subnet.setdefault(node.subnet or "unknown", []).append(node)
        else:
            infra.append(node)

    subnet_sizes = sorted((name, len(members)) for name, members in hosts_by_subnet.items())
    centers = _district_centers(subnet_sizes, spread)

    for subnet, members in hosts_by_subnet.items():
        cx, cz = centers.get(subnet, (0.0, 0.0))
        members.sort(key=lambda n: n.id)
        radius = spread * math.sqrt(max(len(members), 1)) / 2.2
        for i, node in enumerate(members):
            x, z = _disc_position(i, len(members), radius)
            # A little deterministic jitter breaks the perfect spiral, which
            # otherwise reads as a moire pattern at low zoom.
            jx = (_stable_unit(node.id + "x") - 0.5) * jitter
            jz = (_stable_unit(node.id + "z") - 0.5) * jitter
            jy = (_stable_unit(node.id + "y") - 0.5) * jitter * 2
            node.position = Vec3(x=cx + x + jx, y=LAYER_HEIGHT[0] + jy, z=cz + z + jz)

    # Infrastructure sits above the centroid of whatever it connects to, so an
    # access switch lands over its own district instead of floating anywhere.
    adjacency: dict[str, set[str]] = {n.id: set() for n in nodes}
    for link in links:
        if link.source in adjacency and link.target in adjacency:
            adjacency[link.source].add(link.target)
            adjacency[link.target].add(link.source)
    positioned = {n.id: n for n in nodes if n.layer == 0}

    for layer in (1, 2, 3):
        tier = sorted((n for n in infra if n.layer == layer), key=lambda n: n.id)
        for i, node in enumerate(tier):
            anchors = [positioned[p].position for p in adjacency[node.id] if p in positioned]
            if anchors:
                cx = sum(p.x for p in anchors) / len(anchors)
                cz = sum(p.z for p in anchors) / len(anchors)
            elif node.subnet in centers:
                cx, cz = centers[node.subnet]
            else:
                # Top tier with nothing beneath it yet: ring around the origin.
                angle = 2 * math.pi * i / max(len(tier), 1)
                spine = spread * 1.2 * (layer - 1)
                cx, cz = spine * math.cos(angle), spine * math.sin(angle)
            node.position = Vec3(x=cx, y=LAYER_HEIGHT[layer], z=cz)
            positioned[node.id] = node

    return topology


def bounds(nodes: Iterable[Node]) -> dict:
    """Axis-aligned bounds, used by the client to frame the initial camera."""
    xs = [n.position.x for n in nodes] or [0.0]
    ys = [n.position.y for n in nodes] or [0.0]
    zs = [n.position.z for n in nodes] or [0.0]
    return {
        "min": {"x": min(xs), "y": min(ys), "z": min(zs)},
        "max": {"x": max(xs), "y": max(ys), "z": max(zs)},
        "center": {"x": (min(xs) + max(xs)) / 2, "y": (min(ys) + max(ys)) / 2,
                   "z": (min(zs) + max(zs)) / 2},
        "radius": max(
            max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)
        ) / 2 or 1.0,
    }
