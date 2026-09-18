"""Redis-backed bus tests.  Skipped automatically when Redis is not running."""
import os
import uuid

import pytest

from cybernexus.bus import RedisStreamBus

REDIS_URL = os.getenv("CN_REDIS_URL", "redis://localhost:6379/0")


def _redis_available() -> bool:
    try:
        return RedisStreamBus(REDIS_URL).ping()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _redis_available(), reason="Redis not reachable")


@pytest.fixture
def bus():
    b = RedisStreamBus(REDIS_URL, maxlen=10_000)
    yield b
    b.close()


@pytest.fixture
def stream(bus):
    name = f"t:redis:{uuid.uuid4().hex[:8]}"
    yield name
    bus.delete(name)


def test_publish_and_consume(bus, stream):
    bus.publish(stream, [b"a", b"b", b"c"])
    assert bus.length(stream) == 3
    got = bus.consume(stream, "g", "c1", 10, 100)
    assert [p for _, p in got] == [b"a", b"b", b"c"]


def test_group_is_created_idempotently(bus, stream):
    bus.ensure_group(stream, "g")
    bus.ensure_group(stream, "g")
    bus.publish(stream, [b"x"])
    assert len(bus.consume(stream, "g", "c", 10, 100)) == 1


def test_work_is_split_across_consumers(bus, stream):
    bus.publish(stream, [bytes([i]) for i in range(20)])
    a = bus.consume(stream, "g", "c1", 8, 100)
    b = bus.consume(stream, "g", "c2", 20, 100)
    assert len(a) == 8 and len(b) == 12
    assert {p for _, p in a}.isdisjoint({p for _, p in b})


def test_unacked_messages_can_be_reclaimed(bus, stream):
    """A worker that dies mid-batch must not lose the batch."""
    bus.publish(stream, [b"orphan"])
    bus.consume(stream, "g", "dead-worker", 10, 100)      # read, never acked
    reclaimed = bus.claim_stale(stream, "g", "new-worker", min_idle_ms=0, count=10)
    assert [p for _, p in reclaimed] == [b"orphan"]


def test_ack_removes_from_pending(bus, stream):
    bus.publish(stream, [b"a"])
    got = bus.consume(stream, "g", "c", 10, 100)
    bus.ack(stream, "g", [mid for mid, _ in got])
    assert bus.claim_stale(stream, "g", "c2", min_idle_ms=0, count=10) == []


def test_blocking_read_returns_empty_on_timeout(bus, stream):
    assert bus.consume(stream, "g", "c", 10, 50) == []


def test_tail_returns_newest_first(bus, stream):
    bus.publish(stream, [b"1", b"2", b"3"])
    assert [p for _, p in bus.tail(stream, 2)] == [b"3", b"2"]
