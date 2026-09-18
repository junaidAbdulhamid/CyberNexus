import threading
import time

import pytest

from cybernexus.bus import MemoryBus, make_bus


@pytest.fixture
def bus():
    return MemoryBus(maxlen=1000)


def test_publish_and_consume(bus):
    bus.publish("s", [b"a", b"b", b"c"])
    got = bus.consume("s", "g", "c1", 10, 10)
    assert [payload for _, payload in got] == [b"a", b"b", b"c"]


def test_consume_respects_count(bus):
    bus.publish("s", [b"1", b"2", b"3"])
    assert len(bus.consume("s", "g", "c", 2, 10)) == 2
    assert len(bus.consume("s", "g", "c", 2, 10)) == 1


def test_cursor_is_per_group(bus):
    bus.publish("s", [b"x"])
    assert len(bus.consume("s", "g1", "c", 10, 10)) == 1
    assert len(bus.consume("s", "g2", "c", 10, 10)) == 1


def test_two_consumers_in_one_group_split_work(bus):
    bus.publish("s", [bytes([i]) for i in range(10)])
    a = bus.consume("s", "g", "c1", 4, 10)
    b = bus.consume("s", "g", "c2", 10, 10)
    assert len(a) == 4 and len(b) == 6
    assert {p for _, p in a}.isdisjoint({p for _, p in b})


def test_ack_clears_pending(bus):
    bus.publish("s", [b"a", b"b"])
    got = bus.consume("s", "g", "c", 10, 10)
    assert bus.pending("s", "g") == 2
    bus.ack("s", "g", [mid for mid, _ in got])
    assert bus.pending("s", "g") == 0


def test_blocking_read_times_out(bus):
    t0 = time.monotonic()
    assert bus.consume("s", "g", "c", 10, 100) == []
    assert time.monotonic() - t0 >= 0.09


def test_blocking_read_wakes_on_publish(bus):
    def publish_later():
        time.sleep(0.05)
        bus.publish("s", [b"late"])

    threading.Thread(target=publish_later, daemon=True).start()
    got = bus.consume("s", "g", "c", 10, 2000)
    assert [p for _, p in got] == [b"late"]


def test_trimming_drops_oldest_and_cursor_stays_valid():
    bus = MemoryBus(maxlen=4)
    bus.publish("s", [bytes([i]) for i in range(10)])
    got = bus.consume("s", "g", "c", 10, 10)
    assert [p[0] for _, p in got] == [6, 7, 8, 9]
    bus.publish("s", [b"\x0a"])
    assert [p[0] for _, p in bus.consume("s", "g", "c", 10, 10)] == [10]


def test_length_and_tail(bus):
    bus.publish("s", [b"a", b"b", b"c"])
    assert bus.length("s") == 3
    assert [p for _, p in bus.tail("s", 2)] == [b"c", b"b"]


def test_delete_resets_stream(bus):
    bus.publish("s", [b"a"])
    bus.delete("s")
    assert bus.length("s") == 0


def test_publish_empty_is_noop(bus):
    bus.publish("s", [])
    assert bus.length("s") == 0


def test_factory_returns_memory_bus():
    assert isinstance(make_bus("memory"), MemoryBus)
    with pytest.raises(ValueError):
        make_bus("carrier-pigeon")


def test_memory_bus_singleton_is_shared():
    a = MemoryBus.instance("shared-test")
    b = MemoryBus.instance("shared-test")
    assert a is b
