"""Outbound webhook / ticketing stub.

Deliberately a stub, and deliberately honest about it: raising a real ticket
means an authenticated call to a specific Jira/ServiceNow/PagerDuty instance
with a schema those systems own.  What is implemented here is everything up to
that boundary — payload construction, delivery, retry with backoff, and a
delivery log the UI can show — so wiring a real endpoint is configuration, not
code.

Payload contents are limited to topology and alert *metadata*.  No packet
payloads reach this path, because none exist in the system to begin with.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from cnmap.models import Alert, NodeState, Node

log = logging.getLogger(__name__)


@dataclass
class Delivery:
    ts: float
    url: str
    status: str            # "ok" | "failed" | "dry-run"
    http_status: Optional[int] = None
    error: str = ""
    attempts: int = 1
    ticket_ref: str = ""


def build_ticket_payload(node: Node, state: NodeState, alerts: list[Alert],
                         source: str = "cybernexus-map") -> dict:
    """A generic incident payload most ticketing systems can map onto."""
    top = alerts[0] if alerts else None
    return {
        "source": source,
        "created_at": time.time(),
        "severity": state.severity.value,
        "title": f"[{state.severity.value.upper()}] Suspicious activity on {node.label}",
        "summary": (
            f"{state.alert_count} alert(s), peak score {state.score:.2f}. "
            f"Latest: {top.summary if top else 'n/a'}"
        ),
        "asset": {
            "node_id": node.id, "name": node.name, "ip": node.ip, "mac": node.mac,
            "vendor": node.vendor, "device_type": node.device_type.value,
            "subnet": node.subnet, "site": node.site,
            "criticality": node.criticality, "tags": node.tags,
        },
        "detection": {
            "alert_count": state.alert_count,
            "alert_count_window": state.alert_count_window,
            "first_alert_at": state.first_alert_at,
            "last_alert_at": state.last_alert_at,
            "peak_score": state.score,
            "top_ports": state.top_ports,
            "top_peers": state.top_peers,
        },
        "evidence": [
            {"flow_id": a.flow_id, "src_ip": a.src_ip, "dst_ip": a.dst_ip,
             "dst_port": a.dst_port, "protocol": a.protocol, "score": a.score,
             "detected_at": a.detected_at}
            for a in alerts[:20]
        ],
    }


@dataclass
class WebhookSender:
    """Fire-and-log webhook delivery with bounded retries."""

    url: str = ""
    token: str = ""
    timeout: float = 5.0
    max_attempts: int = 3
    dry_run: bool = False
    log_size: int = 200
    deliveries: deque = field(default_factory=lambda: deque(maxlen=200))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def configured(self) -> bool:
        return bool(self.url) or self.dry_run

    def send(self, payload: dict) -> Delivery:
        if not self.url or self.dry_run:
            # A dry run still records exactly what would have been sent, which
            # is what makes the stub reviewable.
            delivery = Delivery(ts=time.time(), url=self.url or "(unset)",
                                status="dry-run", ticket_ref=f"DRY-{int(time.time())}")
            self._record(delivery, payload)
            return delivery

        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "cybernexus-map/1.0"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    ref = ""
                    try:
                        ref = str(json.loads(response.read().decode()).get("id", ""))
                    except Exception:
                        pass
                    delivery = Delivery(ts=time.time(), url=self.url, status="ok",
                                        http_status=response.status, attempts=attempt,
                                        ticket_ref=ref)
                    self._record(delivery, payload)
                    return delivery
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}"
                if exc.code < 500:
                    break                      # client error: retrying will not help
            except Exception as exc:
                last_error = str(exc)
            if attempt < self.max_attempts:
                time.sleep(min(0.5 * 2 ** (attempt - 1), 4.0))

        delivery = Delivery(ts=time.time(), url=self.url, status="failed",
                            error=last_error, attempts=self.max_attempts)
        self._record(delivery, payload)
        log.warning("webhook delivery failed after %d attempts: %s", self.max_attempts, last_error)
        return delivery

    def send_async(self, payload: dict) -> None:
        threading.Thread(target=self.send, args=(payload,), daemon=True).start()

    def _record(self, delivery: Delivery, payload: dict) -> None:
        with self._lock:
            self.deliveries.append({
                "ts": delivery.ts, "url": delivery.url, "status": delivery.status,
                "http_status": delivery.http_status, "error": delivery.error,
                "attempts": delivery.attempts, "ticket_ref": delivery.ticket_ref,
                "title": payload.get("title", ""),
                "node_id": payload.get("asset", {}).get("node_id", ""),
            })

    def recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return list(self.deliveries)[-limit:][::-1]
