"""Synthetic Stage 1 alerts, for the demo and the MTTI harness.

Two uses, one generator:

* **demo** — a steady trickle of low-grade noise so the map looks like a live
  network rather than a still image, with occasional incidents;
* **MTTI harness** — deterministic injection of one incident on one named node
  at a known instant, which is the event whose identification time is measured.

Alerts are published in Stage 1's own wire format to Stage 1's own stream, so
the demo exercises the real ingestion path — codec, consumer group, correlator,
store, WebSocket — rather than a test-only shortcut.  If the simulator works and
Stage 1 does not, the bug is in Stage 1, not in the plumbing.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from cnmap.models import Topology

from .alert_codec import STREAM_FIELD, pack_alert_batch

#: Attack shapes, mirroring the classes Stage 1's model is trained to separate.
SCENARIOS = {
    "port_scan": {
        "ports": [22, 23, 80, 135, 139, 443, 445, 3389, 8080, 5900],
        "score": (0.88, 0.99), "burst": (8, 20), "external": False,
        "description": "host sweeping ports across the subnet",
    },
    "c2_beacon": {
        "ports": [443, 8443, 53, 8080], "score": (0.90, 0.99), "burst": (3, 6),
        "external": True, "description": "periodic check-in to an external host",
    },
    "exfiltration": {
        "ports": [443, 22, 21, 4444], "score": (0.93, 0.995), "burst": (2, 5),
        "external": True, "description": "large outbound transfer",
    },
    "brute_force": {
        "ports": [22, 3389, 445], "score": (0.85, 0.97), "burst": (10, 25),
        "external": False, "description": "repeated authentication attempts",
    },
    "lateral_movement": {
        "ports": [445, 3389, 5985, 22], "score": (0.86, 0.98), "burst": (5, 12),
        "external": False, "description": "spreading to internal neighbours",
    },
}

#: Low-severity background chatter.  Real sensors are never silent, and an
#: operator's job is finding the signal in this, so the MTTI comparison is
#: meaningless without it.
NOISE_PORTS = [80, 443, 53, 123, 8080, 3128]


@dataclass
class AlertSimulator:
    topology: Topology
    seed: int = 1234
    rng: random.Random = field(init=False)
    _hosts: list = field(init=False, default_factory=list)
    _host_ips: list = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        self._hosts = [
            n for n in self.topology.nodes
            if n.ip and n.device_type.value in
            {"workstation", "server", "printer", "iot"}
        ]
        self._host_ips = [n.ip for n in self._hosts]
        if not self._hosts:
            raise ValueError("topology has no end hosts to generate alerts for")

    # -- primitives ------------------------------------------------------
    def _external_ip(self) -> str:
        return f"203.0.{self.rng.randint(1, 254)}.{self.rng.randint(1, 254)}"

    def _alert(self, src_ip: str, dst_ip: str, dst_port: int, score: float,
               now: Optional[float] = None) -> dict:
        now = now if now is not None else time.time()
        return {
            "flow_id": f"sim-{self.rng.randrange(1 << 40):010x}",
            "src_ip": src_ip, "dst_ip": dst_ip,
            "src_port": self.rng.randint(49152, 65535), "dst_port": dst_port,
            "protocol": 6, "score": round(score, 4), "threshold": 0.2158,
            "detected_at": now, "latency_s": round(self.rng.uniform(0.004, 0.02), 4),
            "label": 1.0 if score >= 0.5 else 0.0,
        }

    def noise(self, count: int = 1, now: Optional[float] = None) -> list[dict]:
        """Benign-looking, low-score alerts: the haystack."""
        out = []
        for _ in range(count):
            src = self.rng.choice(self._host_ips)
            dst = self._external_ip() if self.rng.random() < 0.6 else self.rng.choice(self._host_ips)
            out.append(self._alert(src, dst, self.rng.choice(NOISE_PORTS),
                                   self.rng.uniform(0.22, 0.45), now))
        return out

    def incident(self, node_id: Optional[str] = None, scenario: Optional[str] = None,
                 now: Optional[float] = None) -> tuple[str, str, list[dict]]:
        """A burst of high-severity alerts for one host.

        Returns ``(node_id, scenario, alerts)`` so a harness knows exactly which
        node the operator is supposed to find.
        """
        scenario = scenario or self.rng.choice(list(SCENARIOS))
        spec = SCENARIOS[scenario]
        if node_id:
            node = next((n for n in self._hosts if n.id == node_id), None)
            if node is None:
                raise ValueError(f"no such host node: {node_id}")
        else:
            node = self.rng.choice(self._hosts)

        low, high = spec["score"]
        burst = self.rng.randint(*spec["burst"])
        alerts = []
        for _ in range(burst):
            port = self.rng.choice(spec["ports"])
            peer = self._external_ip() if spec["external"] else self.rng.choice(self._host_ips)
            alerts.append(self._alert(node.ip, peer, port, self.rng.uniform(low, high), now))
        return node.id, scenario, alerts

    # -- publishing ------------------------------------------------------
    def publish(self, redis_client, alerts: Iterable[dict], stream: str = "cn:alerts",
                maxlen: int = 100_000) -> int:
        alerts = list(alerts)
        if not alerts:
            return 0
        redis_client.xadd(stream, {STREAM_FIELD: pack_alert_batch(alerts)},
                          maxlen=maxlen, approximate=True)
        return len(alerts)

    def run(self, redis_client, duration_s: float = 60.0, noise_per_s: float = 2.0,
            incident_every_s: float = 20.0, stream: str = "cn:alerts",
            on_incident: Optional[Callable[[str, str], None]] = None) -> dict:
        """Drive a live demo: background noise plus periodic incidents."""
        start = time.time()
        deadline = start + duration_s if duration_s else float("inf")
        next_incident = start + incident_every_s
        published = incidents = 0
        interval = 1.0 / max(noise_per_s, 0.01)

        while time.time() < deadline:
            published += self.publish(redis_client, self.noise(1), stream)
            now = time.time()
            if now >= next_incident:
                node_id, scenario, alerts = self.incident()
                published += self.publish(redis_client, alerts, stream)
                incidents += 1
                next_incident = now + incident_every_s
                if on_incident:
                    on_incident(node_id, scenario)
            time.sleep(interval)
        return {"published": published, "incidents": incidents,
                "elapsed_s": time.time() - start}


def main(argv=None) -> None:
    import argparse
    import logging

    import redis as redis_lib

    p = argparse.ArgumentParser(description="Publish synthetic Stage 1 alerts")
    p.add_argument("--topology", default="data/example-topology.json")
    p.add_argument("--redis-url", default="redis://localhost:6379/0")
    p.add_argument("--stream", default="cn:alerts")
    p.add_argument("--duration", type=float, default=0.0, help="0 = run forever")
    p.add_argument("--noise-rate", type=float, default=2.0, help="alerts/second")
    p.add_argument("--incident-every", type=float, default=20.0)
    p.add_argument("--seed", type=int, default=1234)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    topology = Topology.model_validate_json(open(args.topology).read())
    sim = AlertSimulator(topology, seed=args.seed)
    client = redis_lib.Redis.from_url(args.redis_url)
    logging.info("publishing synthetic alerts to %s (%s)", args.stream, args.redis_url)
    result = sim.run(
        client, duration_s=args.duration, noise_per_s=args.noise_rate,
        incident_every_s=args.incident_every, stream=args.stream,
        on_incident=lambda node_id, scenario: logging.info("incident: %s on %s", scenario, node_id),
    )
    logging.info("done: %s", result)


if __name__ == "__main__":
    main()
