"""Process metrics: Prometheus counters plus a cheap local rate meter.

Every service exports the same three families so a dashboard does not need to
know which stage it is looking at.  ``prometheus_client`` is optional; without
it the no-op shims keep the hot loop free of ``if metrics_enabled`` branches.
"""
from __future__ import annotations

import os
import time
from collections import deque

try:  # pragma: no cover - exercised by whichever branch the env provides
    from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
    from prometheus_client import start_http_server as _start_http_server

    HAVE_PROMETHEUS = True
except ImportError:  # pragma: no cover
    HAVE_PROMETHEUS = False

    class _Noop:
        def labels(self, *a, **k):
            return self

        def inc(self, *a, **k):
            pass

        def set(self, *a, **k):
            pass

        def observe(self, *a, **k):
            pass

    Counter = Gauge = Histogram = lambda *a, **k: _Noop()  # type: ignore
    CollectorRegistry = object  # type: ignore

    def generate_latest(*a, **k):  # type: ignore
        return b""

    def _start_http_server(*a, **k):  # type: ignore
        pass


STAGE = os.getenv("CN_STAGE", "unknown")

RECORDS = Counter("cn_records_total", "Records processed", ["stage"])
BATCHES = Counter("cn_batches_total", "Batches processed", ["stage"])
ALERTS = Counter("cn_alerts_total", "Alerts raised", ["stage"])
ERRORS = Counter("cn_errors_total", "Errors", ["stage", "kind"])
RATE = Gauge("cn_records_per_second", "Recent throughput", ["stage"])
LAG = Gauge("cn_stream_backlog", "Unconsumed stream entries", ["stage", "stream"])
LATENCY = Histogram(
    "cn_stage_latency_seconds",
    "Per-batch processing latency",
    ["stage"],
    buckets=(0.0005, 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)
E2E = Histogram(
    "cn_end_to_end_latency_seconds",
    "Connection emit -> verdict",
    ["stage"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)


def start_metrics_server(port: int) -> None:
    if HAVE_PROMETHEUS and port:
        _start_http_server(port)


class RateMeter:
    """Sliding-window throughput, for log lines and the bench harness.

    Deliberately allocation-free on the hot path: a deque of (t, n) pairs that
    is trimmed on read, not a full histogram.
    """

    def __init__(self, window_s: float = 5.0):
        self.window_s = window_s
        self._events: deque[tuple[float, int]] = deque()
        self.total = 0
        self.started = time.monotonic()

    def add(self, n: int) -> None:
        self.total += n
        self._events.append((time.monotonic(), n))

    def rate(self) -> float:
        now = time.monotonic()
        cutoff = now - self.window_s
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        if not self._events:
            return 0.0
        span = max(now - self._events[0][0], 1e-6)
        return sum(n for _, n in self._events) / span

    def overall(self) -> float:
        elapsed = max(time.monotonic() - self.started, 1e-6)
        return self.total / elapsed
