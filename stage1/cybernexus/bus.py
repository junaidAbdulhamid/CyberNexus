"""Message-bus abstraction.

Two implementations share one interface:

``RedisStreamBus``
    The default.  Redis Streams give us consumer groups (horizontal scaling),
    at-least-once delivery with explicit acks, a pending-entries list for crash
    recovery, and approximate trimming to bound memory — everything the
    pipeline needs without operating a Kafka cluster.  See ``DESIGN.md`` for the
    Kafka variant and when to switch.

``MemoryBus``
    An in-process implementation with identical semantics used by the unit and
    integration tests and by ``bench --bus memory``, so the whole pipeline can
    be exercised on a laptop with no external services.

Payloads are opaque ``bytes`` (msgpack batches from :mod:`cybernexus.records`);
the bus never inspects them.
"""
from __future__ import annotations

import abc
import itertools
import threading
import time
from collections import deque
from typing import Iterable, Sequence

#: Single field name used inside every stream entry.  Short on purpose: it is
#: repeated once per message on the wire and in the Redis keyspace.
FIELD = b"b"


class Bus(abc.ABC):
    """Minimal stream interface: publish batches, consume batches, ack."""

    @abc.abstractmethod
    def publish(self, stream: str, payloads: Sequence[bytes]) -> None:
        """Append one or more payloads to ``stream`` (pipelined when possible)."""

    @abc.abstractmethod
    def ensure_group(self, stream: str, group: str) -> None:
        """Create the consumer group if it does not exist (idempotent)."""

    @abc.abstractmethod
    def consume(
        self, stream: str, group: str, consumer: str, count: int, block_ms: int
    ) -> list[tuple[bytes, bytes]]:
        """Return up to ``count`` ``(message_id, payload)`` pairs."""

    @abc.abstractmethod
    def ack(self, stream: str, group: str, ids: Sequence[bytes]) -> None:
        ...

    @abc.abstractmethod
    def length(self, stream: str) -> int:
        ...

    @abc.abstractmethod
    def tail(self, stream: str, count: int) -> list[tuple[bytes, bytes]]:
        """Most recent ``count`` entries, newest first (read-only inspection)."""

    def close(self) -> None:  # pragma: no cover - trivial
        pass


class RedisStreamBus(Bus):
    def __init__(self, url: str, maxlen: int = 2_000_000):
        import redis  # imported lazily so MemoryBus users need no redis package

        self.url = url
        self.maxlen = maxlen
        self._r = redis.Redis.from_url(url)
        self._groups: set[tuple[str, str]] = set()

    @property
    def client(self):
        return self._r

    def publish(self, stream: str, payloads: Sequence[bytes]) -> None:
        if not payloads:
            return
        if len(payloads) == 1:
            self._r.xadd(stream, {FIELD: payloads[0]}, maxlen=self.maxlen, approximate=True)
            return
        pipe = self._r.pipeline(transaction=False)
        for payload in payloads:
            pipe.xadd(stream, {FIELD: payload}, maxlen=self.maxlen, approximate=True)
        pipe.execute()

    def ensure_group(self, stream: str, group: str) -> None:
        key = (stream, group)
        if key in self._groups:
            return
        import redis

        try:
            self._r.xgroup_create(stream, group, id="0", mkstream=True)
        except redis.ResponseError as exc:  # BUSYGROUP -> already exists
            if "BUSYGROUP" not in str(exc):
                raise
        self._groups.add(key)

    def consume(
        self, stream: str, group: str, consumer: str, count: int, block_ms: int
    ) -> list[tuple[bytes, bytes]]:
        self.ensure_group(stream, group)
        resp = self._r.xreadgroup(
            group, consumer, {stream: ">"}, count=count, block=block_ms
        )
        out: list[tuple[bytes, bytes]] = []
        for _stream, messages in resp or ():
            for msg_id, fields in messages:
                payload = fields.get(FIELD)
                if payload is not None:
                    out.append((msg_id, payload))
        return out

    def claim_stale(
        self, stream: str, group: str, consumer: str, min_idle_ms: int, count: int
    ) -> list[tuple[bytes, bytes]]:
        """Recover messages a dead consumer never acked (at-least-once delivery)."""
        self.ensure_group(stream, group)
        try:
            _, messages, _ = self._r.xautoclaim(
                stream, group, consumer, min_idle_time=min_idle_ms, count=count
            )
        except Exception:  # pragma: no cover - server < 6.2
            return []
        return [(mid, f[FIELD]) for mid, f in messages if FIELD in f]

    def ack(self, stream: str, group: str, ids: Sequence[bytes]) -> None:
        if ids:
            self._r.xack(stream, group, *ids)

    def length(self, stream: str) -> int:
        try:
            return int(self._r.xlen(stream))
        except Exception:
            return 0

    def tail(self, stream: str, count: int) -> list[tuple[bytes, bytes]]:
        entries = self._r.xrevrange(stream, max="+", min="-", count=count)
        return [(mid, f[FIELD]) for mid, f in entries if FIELD in f]

    def trim(self, stream: str, maxlen: int = 0) -> None:
        self._r.xtrim(stream, maxlen=maxlen, approximate=False)

    def delete(self, *streams: str) -> None:
        if streams:
            self._r.delete(*streams)

    def ping(self) -> bool:
        try:
            return bool(self._r.ping())
        except Exception:
            return False

    def close(self) -> None:
        try:
            self._r.close()
        except Exception:  # pragma: no cover
            pass


class MemoryBus(Bus):
    """Thread-safe in-process stand-in with Redis-Streams-like semantics.

    Supports multiple consumer groups per stream, per-group cursors, explicit
    acks and blocking reads.  Entries are kept in a bounded deque; a group that
    falls far enough behind loses the oldest entries exactly as ``MAXLEN``
    trimming would drop them in Redis.
    """

    _shared: dict[str, "MemoryBus"] = {}

    def __init__(self, maxlen: int = 2_000_000):
        self.maxlen = maxlen
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._streams: dict[str, deque[tuple[bytes, bytes]]] = {}
        # Number of entries ever evicted from each stream's head, so that a
        # group cursor stays correct across maxlen trimming.
        self._evicted: dict[str, int] = {}
        self._cursors: dict[tuple[str, str], int] = {}
        self._pending: dict[tuple[str, str], dict[bytes, bytes]] = {}
        self._seq = itertools.count(1)

    @classmethod
    def instance(cls, name: str = "default", maxlen: int = 2_000_000) -> "MemoryBus":
        """Process-wide singleton so separate threads share one bus."""
        bus = cls._shared.get(name)
        if bus is None:
            bus = cls._shared[name] = cls(maxlen=maxlen)
        return bus

    def _stream(self, stream: str) -> deque:
        dq = self._streams.get(stream)
        if dq is None:
            dq = self._streams[stream] = deque(maxlen=self.maxlen)
            self._evicted[stream] = 0
        return dq

    def publish(self, stream: str, payloads: Sequence[bytes]) -> None:
        if not payloads:
            return
        with self._cv:
            dq = self._stream(stream)
            for payload in payloads:
                seq = next(self._seq)
                if self.maxlen and len(dq) == self.maxlen:
                    self._evicted[stream] += 1
                dq.append((f"{seq}-0".encode(), payload))
            self._cv.notify_all()

    def ensure_group(self, stream: str, group: str) -> None:
        with self._cv:
            self._stream(stream)
            self._cursors.setdefault((stream, group), 0)
            self._pending.setdefault((stream, group), {})

    def consume(
        self, stream: str, group: str, consumer: str, count: int, block_ms: int
    ) -> list[tuple[bytes, bytes]]:
        deadline = time.monotonic() + block_ms / 1000.0
        self.ensure_group(stream, group)
        key = (stream, group)
        while True:
            with self._cv:
                dq = self._stream(stream)
                evicted = self._evicted[stream]
                # The cursor counts entries ever appended, so subtracting the
                # evicted count maps it onto the current deque index.  A group
                # that fell behind resumes at the oldest surviving entry.
                idx = max(self._cursors[key] - evicted, 0)
                taken: list[tuple[bytes, bytes]] = []
                while idx < len(dq) and len(taken) < count:
                    entry = dq[idx]
                    taken.append(entry)
                    self._pending[key][entry[0]] = entry[1]
                    idx += 1
                self._cursors[key] = evicted + idx
                if taken:
                    return taken
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._cv.wait(timeout=remaining)

    def ack(self, stream: str, group: str, ids: Sequence[bytes]) -> None:
        key = (stream, group)
        with self._cv:
            pending = self._pending.setdefault(key, {})
            for mid in ids:
                pending.pop(mid, None)

    def length(self, stream: str) -> int:
        with self._cv:
            return len(self._stream(stream))

    def tail(self, stream: str, count: int) -> list[tuple[bytes, bytes]]:
        with self._cv:
            dq = self._stream(stream)
            return list(itertools.islice(reversed(dq), count))

    def pending(self, stream: str, group: str) -> int:
        with self._cv:
            return len(self._pending.get((stream, group), {}))

    def delete(self, *streams: str) -> None:
        with self._cv:
            for s in streams:
                self._streams.pop(s, None)
                self._evicted.pop(s, None)
                for key in list(self._cursors):
                    if key[0] == s:
                        self._cursors.pop(key, None)
                        self._pending.pop(key, None)

    def ping(self) -> bool:
        return True


def make_bus(kind: str | None = None, url: str | None = None, maxlen: int | None = None) -> Bus:
    """Factory used by every service: ``CN_BUS=redis|memory``."""
    from .config import settings

    kind = (kind or settings.bus).lower()
    maxlen = maxlen if maxlen is not None else settings.stream_maxlen
    if kind in {"memory", "mem", "inproc"}:
        return MemoryBus.instance(maxlen=maxlen)
    if kind == "redis":
        return RedisStreamBus(url or settings.redis_url, maxlen=maxlen)
    raise ValueError(f"unknown bus kind: {kind!r}")


def drain(bus: Bus, stream: str, group: str, consumer: str, count: int, block_ms: int) -> Iterable[tuple[bytes, bytes]]:
    """Convenience generator used by tests."""
    while True:
        batch = bus.consume(stream, group, consumer, count, block_ms)
        if not batch:
            return
        yield from batch
