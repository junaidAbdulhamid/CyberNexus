"""WebSocket fan-out.

One process, many browsers.  The hub owns the client set and the broadcast
path, and it is written around two failure modes that matter in a SOC:

* **a slow client must not stall the others** — each client has a bounded queue
  and is dropped if it falls too far behind, rather than letting a wedged tab
  apply back-pressure to the alert pipeline;
* **updates must coalesce** — during an incident, one node can generate dozens
  of alerts a second.  The hub batches whatever accumulated in the last tick
  into a single frame, so the client renders once per tick instead of once per
  alert, and the wire carries one message instead of fifty.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)

#: Frames a client may fall behind before it is disconnected.
CLIENT_QUEUE_LIMIT = 256


# eq=False keeps the default identity-based __hash__: clients live in a set,
# and a dataclass with the default eq=True sets __hash__ to None.
@dataclass(eq=False)
class Client:
    websocket: Any
    principal: dict
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=CLIENT_QUEUE_LIMIT))
    connected_at: float = field(default_factory=time.time)
    sent: int = 0
    dropped: bool = False


class Hub:
    def __init__(self, flush_interval_s: float = 0.05):
        self.clients: set[Client] = set()
        self.flush_interval_s = flush_interval_s
        self._pending: list[dict] = []
        self._lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.frames_sent = 0
        self.updates_queued = 0

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the event loop so background threads can publish into it."""
        self._loop = loop

    # -- membership ------------------------------------------------------
    async def connect(self, websocket, principal: dict) -> Client:
        client = Client(websocket=websocket, principal=principal)
        self.clients.add(client)
        log.info("ws client connected (%d total, sub=%s)", len(self.clients), principal.get("sub"))
        return client

    async def disconnect(self, client: Client) -> None:
        self.clients.discard(client)
        log.info("ws client disconnected (%d left)", len(self.clients))

    # -- publishing ------------------------------------------------------
    def publish_threadsafe(self, message: dict) -> None:
        """Queue an update from a non-async thread (the Stage 1 connector)."""
        if self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._enqueue, message)

    def _enqueue(self, message: dict) -> None:
        self._pending.append(message)
        self.updates_queued += 1

    async def publish(self, message: dict) -> None:
        self._enqueue(message)

    async def broadcast_now(self, frame: dict) -> None:
        """Send one frame immediately, bypassing the coalescing tick."""
        payload = json.dumps(frame, default=str)
        for client in list(self.clients):
            self._offer(client, payload)

    def _offer(self, client: Client, payload: str) -> None:
        try:
            client.queue.put_nowait(payload)
        except asyncio.QueueFull:
            # The client is not keeping up.  Mark it; the writer task will close
            # it.  Dropping one browser beats degrading the whole map.
            client.dropped = True
            log.warning("ws client is too slow; dropping it")

    async def flush_loop(self) -> None:
        """Coalesce pending updates into one frame per tick."""
        while True:
            await asyncio.sleep(self.flush_interval_s)
            if not self._pending:
                continue
            batch, self._pending = self._pending, []
            frame = json.dumps(
                {"type": "batch", "ts": time.time(), "updates": batch}, default=str
            )
            self.frames_sent += 1
            for client in list(self.clients):
                self._offer(client, frame)

    async def writer(self, client: Client) -> None:
        """Per-client send loop."""
        try:
            while True:
                if client.dropped:
                    await client.websocket.close(code=1013, reason="client too slow")
                    return
                payload = await client.queue.get()
                await client.websocket.send_text(payload)
                client.sent += 1
        except Exception:
            return

    def stats(self) -> dict:
        return {
            "clients": len(self.clients),
            "frames_sent": self.frames_sent,
            "updates_queued": self.updates_queued,
            "pending": len(self._pending),
        }
