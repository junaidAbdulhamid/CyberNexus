"""Subscribe to the Stage 1 alert stream and drive the map.

Runs as a background thread inside the backend (or standalone, for debugging).
It is a Redis Streams *consumer group* member, exactly like Stage 1's own
workers, which buys the same two properties: several backend replicas can share
the load, and an alert that was read but not processed is redelivered rather
than lost.

The connector does three things and nothing else: read, correlate-and-store,
notify.  Broadcasting to WebSocket clients is the backend's job, handed over
through the ``on_update`` callback, so this module has no opinion about
transport and is testable with a plain list as the sink.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from cnmap.models import Alert, NodeStateUpdate
from cnmap.store import MapStore

from .alert_codec import STREAM_FIELD, decode_entry

log = logging.getLogger(__name__)

UpdateCallback = Callable[[NodeStateUpdate], None]


class Stage1Connector:
    """Reads ``cn:alerts`` and folds each alert into the map store."""

    def __init__(
        self,
        store: MapStore,
        redis_client,
        stream: str = "cn:alerts",
        group: str = "cnmap",
        consumer: str = "cnmap-1",
        on_update: Optional[UpdateCallback] = None,
        batch_size: int = 64,
        block_ms: int = 500,
        start_id: str = "$",
    ):
        self.store = store
        self.redis = redis_client
        self.stream = stream
        self.group = group
        self.consumer = consumer
        self.on_update = on_update
        self.batch_size = batch_size
        self.block_ms = block_ms
        # "$" = only alerts from now on.  A map that replays yesterday's alerts
        # on startup shows an operator a network that is not on fire any more.
        self.start_id = start_id
        self.running = False
        self.connected = False
        self.alerts_seen = 0
        self.updates_emitted = 0
        self.last_error: str = ""
        self.last_alert_at: float = 0.0
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle -------------------------------------------------------
    def ensure_group(self) -> bool:
        try:
            self.redis.xgroup_create(self.stream, self.group, id=self.start_id, mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                self.last_error = str(exc)
                return False
        self.connected = True
        return True

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, name="stage1-connector", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self.running = False
        if self._thread:
            self._thread.join(timeout=timeout)

    # -- main loop -------------------------------------------------------
    def _loop(self) -> None:
        backoff = 0.5
        while self.running:
            if not self.connected and not self.ensure_group():
                # Stage 1 may simply not be running yet; keep retrying quietly
                # instead of taking the map down with it.
                log.info("waiting for Stage 1 alert stream (%s)", self.last_error)
                time.sleep(min(backoff, 10.0))
                backoff *= 2
                continue
            backoff = 0.5
            try:
                self.poll_once()
            except Exception as exc:  # pragma: no cover - transport failure
                self.last_error = str(exc)
                self.connected = False
                log.warning("connector error: %s", exc)
                time.sleep(1.0)

    def poll_once(self) -> int:
        """One read/process/ack cycle.  Returns the number of alerts handled."""
        response = self.redis.xreadgroup(
            self.group, self.consumer, {self.stream: ">"},
            count=self.batch_size, block=self.block_ms,
        )
        if not response:
            return 0
        handled = 0
        for _stream, messages in response:
            ids = []
            for message_id, fields in messages:
                ids.append(message_id)
                blob = fields.get(STREAM_FIELD)
                if not blob:
                    continue
                for alert in decode_entry(blob):
                    handled += self.handle_alert(alert)
            if ids:
                self.redis.xack(self.stream, self.group, *ids)
        return handled

    def handle_alert(self, alert: Alert) -> int:
        self.alerts_seen += 1
        self.last_alert_at = time.time()
        update = self.store.apply_alert(alert)
        if update is None:
            return 1          # counted, but it did not map to a node
        self.updates_emitted += 1
        if self.on_update:
            try:
                self.on_update(update)
            except Exception as exc:  # pragma: no cover - sink failure
                log.warning("update callback failed: %s", exc)
        return 1

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "running": self.running,
            "stream": self.stream,
            "group": self.group,
            "alerts_seen": self.alerts_seen,
            "updates_emitted": self.updates_emitted,
            "last_alert_at": self.last_alert_at,
            "last_error": self.last_error,
        }
