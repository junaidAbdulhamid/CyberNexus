"""Shared plumbing for the long-running stage processes.

Each stage (ingest, scorer) is the same shape: join a consumer group, read a
batch, do numpy work, publish downstream, ack.  ``StageRunner`` owns that loop
so the stage modules only contain the part that differs, and so signal handling,
metrics, backlog reporting and pending-message recovery are implemented once.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import time
from typing import Callable, Sequence

from .bus import Bus, RedisStreamBus
from .config import Settings
from .metrics import BATCHES, ERRORS, LAG, LATENCY, RATE, RECORDS, RateMeter, start_metrics_server

log = logging.getLogger(__name__)


def apply_cpu_affinity(spec: str, worker_index: int) -> None:
    """Pin this process to one CPU from ``spec`` (e.g. ``"2,3,4,5"``).

    Pinning keeps a worker's feature matrices in one core's cache and stops the
    scheduler from migrating a busy numpy loop mid-batch.  ``sched_setaffinity``
    is Linux-only; on macOS this is a documented no-op.
    """
    if not spec or not hasattr(os, "sched_setaffinity"):
        return
    cpus = [int(c) for c in spec.replace(" ", "").split(",") if c != ""]
    if not cpus:
        return
    cpu = cpus[worker_index % len(cpus)]
    try:
        os.sched_setaffinity(0, {cpu})
        log.info("pinned worker %d to cpu %d", worker_index, cpu)
    except OSError as exc:  # pragma: no cover - platform dependent
        log.warning("could not set cpu affinity: %s", exc)


class StageRunner:
    """Consume batches from one stream, hand them to ``handler``, ack them."""

    def __init__(
        self,
        name: str,
        bus: Bus,
        settings: Settings,
        in_stream: str,
        group: str,
        handler: Callable[[Sequence[bytes]], int],
        worker_index: int = 0,
        metrics_port: int = 0,
        idle_exit_s: float = 0.0,
    ):
        self.name = name
        self.bus = bus
        self.settings = settings
        self.in_stream = in_stream
        self.group = group
        self.handler = handler
        self.worker_index = worker_index
        self.consumer = f"{name}-{socket.gethostname()}-{os.getpid()}"
        self.idle_exit_s = idle_exit_s
        self.meter = RateMeter()
        self.running = True
        # Wall-clock window in which this stage was actually doing work, so a
        # throughput figure is not diluted by startup or drain-out waiting.
        self.first_batch_ts = 0.0
        self.last_batch_ts = 0.0
        self._last_log = time.monotonic()
        self._last_claim = time.monotonic()
        if metrics_port:
            start_metrics_server(metrics_port + worker_index)

    def install_signal_handlers(self) -> None:
        def stop(signum, _frame):
            log.info("%s: signal %s, draining", self.name, signum)
            self.running = False

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, stop)
            except ValueError:  # pragma: no cover - not on the main thread
                pass

    def _recover_pending(self) -> list[tuple[bytes, bytes]]:
        """Reclaim batches an earlier worker read but never acked."""
        if not isinstance(self.bus, RedisStreamBus):
            return []
        now = time.monotonic()
        if now - self._last_claim < 5.0:
            return []
        self._last_claim = now
        return self.bus.claim_stale(
            self.in_stream, self.group, self.consumer, min_idle_ms=30_000,
            count=self.settings.read_batch,
        )

    def run(self, max_seconds: float = 0.0) -> int:
        self.install_signal_handlers()
        apply_cpu_affinity(self.settings.cpu_affinity, self.worker_index)
        self.bus.ensure_group(self.in_stream, self.group)
        log.info("%s[%d] consuming %s (group=%s)", self.name, self.worker_index,
                 self.in_stream, self.group)
        deadline = time.monotonic() + max_seconds if max_seconds else 0.0
        idle_since = time.monotonic()

        while self.running:
            if deadline and time.monotonic() > deadline:
                break
            try:
                batch = self.bus.consume(
                    self.in_stream, self.group, self.consumer,
                    self.settings.read_batch, self.settings.block_ms,
                )
                if not batch:
                    batch = self._recover_pending()
                if not batch:
                    if self.idle_exit_s and time.monotonic() - idle_since > self.idle_exit_s:
                        log.info("%s[%d] idle for %.1fs, exiting",
                                 self.name, self.worker_index, self.idle_exit_s)
                        break
                    continue
                idle_since = time.monotonic()
                t0 = time.perf_counter()
                if not self.first_batch_ts:
                    self.first_batch_ts = t0
                n = self.handler([payload for _, payload in batch])
                LATENCY.labels(self.name).observe(time.perf_counter() - t0)
                self.last_batch_ts = time.perf_counter()
                self.bus.ack(self.in_stream, self.group, [mid for mid, _ in batch])
                self.meter.add(n)
                RECORDS.labels(self.name).inc(n)
                BATCHES.labels(self.name).inc(len(batch))
                self._maybe_log()
            except KeyboardInterrupt:  # pragma: no cover
                break
            except Exception as exc:  # keep the stage alive; drop the batch
                ERRORS.labels(self.name, type(exc).__name__).inc()
                log.exception("%s: batch failed: %s", self.name, exc)
        log.info("%s[%d] stopped after %d records (%.0f rec/s while active)",
                 self.name, self.worker_index, self.meter.total, self.active_rate())
        return self.meter.total

    def active_rate(self) -> float:
        """Records per second over the window this stage had work to do."""
        span = self.last_batch_ts - self.first_batch_ts
        return self.meter.total / span if span > 0 else self.meter.overall()

    def _maybe_log(self) -> None:
        now = time.monotonic()
        if now - self._last_log < 5.0:
            return
        self._last_log = now
        rate = self.meter.rate()
        RATE.labels(self.name).set(rate)
        backlog = self.bus.length(self.in_stream)
        LAG.labels(self.name, self.in_stream).set(backlog)
        log.info("%s[%d] %.0f rec/s (total %d, stream len %d)",
                 self.name, self.worker_index, rate, self.meter.total, backlog)
