"""Durable worker loop for the workspace queue.

Phase 3 executed audits in an in-process ``ThreadPoolExecutor``. Restarting the
API abandoned any running work. Phase 4 moves execution behind a lease:

1. A worker leases the highest-priority runnable job.
2. A background thread renews the lease while the audit runs.
3. On success the job is finished; on a transient failure it is released back to
   the queue with exponential backoff; when the attempt budget is exhausted it
   becomes a dead letter that an operator can inspect and retry explicitly.

Workers are horizontally scalable and stateless. Killing one mid-audit costs at
most one lease interval, after which any other worker picks the job up.
"""

from __future__ import annotations

import logging
import random
import signal
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class QueueSettings:
    lease_seconds: int = 120
    heartbeat_seconds: int = 20
    poll_seconds: float = 2.0
    max_attempts: int = 3
    backoff_seconds: float = 15.0
    backoff_ceiling_seconds: float = 900.0
    reclaim_every_seconds: float = 30.0

    @classmethod
    def from_config(cls, config) -> "QueueSettings":
        return cls(
            lease_seconds=config.QUEUE_LEASE_SECONDS,
            heartbeat_seconds=config.QUEUE_HEARTBEAT_SECONDS,
            poll_seconds=config.QUEUE_POLL_SECONDS,
            max_attempts=config.QUEUE_MAX_ATTEMPTS,
            backoff_seconds=config.QUEUE_BACKOFF_SECONDS,
            backoff_ceiling_seconds=config.QUEUE_BACKOFF_CEILING_SECONDS,
        )

    def backoff_for(self, attempt: int) -> float:
        """Exponential backoff with jitter, capped so a retry is never lost."""
        base = min(
            self.backoff_ceiling_seconds,
            self.backoff_seconds * (2 ** max(0, attempt - 1)),
        )
        return round(base + random.uniform(0, min(5.0, base / 4)), 3)


class LeaseRenewal:
    """Renew a job lease on a timer for as long as the job is executing."""

    def __init__(self, store, tenant_id: str, job_id: str, worker_id: str,
                 settings: QueueSettings):
        self.store = store
        self.tenant_id = tenant_id
        self.job_id = job_id
        self.worker_id = worker_id
        self.settings = settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost = False

    def __enter__(self) -> "LeaseRenewal":
        self._thread = threading.Thread(
            target=self._run, name=f"lease-{self.job_id[:8]}", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self.settings.heartbeat_seconds):
            try:
                renewed = self.store.heartbeat_job(
                    self.tenant_id,
                    self.job_id,
                    self.worker_id,
                    lease_seconds=self.settings.lease_seconds,
                )
                if not renewed:
                    # Another worker reclaimed the lease, or the job finished.
                    self.lost = True
                    logger.warning("Lease for job %s is no longer held.", self.job_id)
                    return
            except Exception:  # noqa: BLE001 - never kill the audit over a heartbeat
                logger.exception("Lease renewal failed for job %s.", self.job_id)


class DurableQueueWorker:
    """Poll the workspace queue and execute audits until asked to stop."""

    def __init__(
        self,
        store,
        executor,
        *,
        settings: QueueSettings | None = None,
        worker_id: str | None = None,
        kinds: tuple[str, ...] = ("audit",),
    ):
        import socket
        import os

        self.store = store
        self.executor = executor
        self.settings = settings or QueueSettings()
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.kinds = kinds
        self._stop = threading.Event()
        self._last_reclaim = 0.0
        self.processed = 0

    def request_stop(self, *_signal_args) -> None:
        logger.info("Worker %s received a stop request.", self.worker_id)
        self._stop.set()

    def install_signal_handlers(self) -> None:
        for name in ("SIGINT", "SIGTERM"):
            handler = getattr(signal, name, None)
            if handler is not None:
                try:
                    signal.signal(handler, self.request_stop)
                except ValueError:  # pragma: no cover - not on the main thread
                    logger.debug("Signal handlers require the main thread; skipping %s.", name)

    def run_forever(self) -> int:
        logger.info("Worker %s polling for %s jobs.", self.worker_id, ", ".join(self.kinds))
        while not self._stop.is_set():
            try:
                if not self.run_once():
                    self._stop.wait(self.settings.poll_seconds)
            except Exception:  # noqa: BLE001 - a worker must not die on one bad loop
                logger.exception("Worker loop error; continuing after a short pause.")
                self._stop.wait(min(30.0, max(1.0, self.settings.poll_seconds * 5)))
        logger.info("Worker %s stopped after %d jobs.", self.worker_id, self.processed)
        return self.processed

    def run_once(self) -> bool:
        """Execute at most one job. Returns True when work was performed."""
        self._maybe_reclaim()
        job = self.store.lease_next_job(
            self.worker_id, lease_seconds=self.settings.lease_seconds, kinds=self.kinds
        )
        if job is None:
            return False
        tenant_id, job_id = str(job["tenant_id"]), str(job["id"])
        with LeaseRenewal(self.store, tenant_id, job_id, self.worker_id, self.settings):
            self.executor.execute(tenant_id, job)
        self.processed += 1
        return True

    def _maybe_reclaim(self) -> None:
        now = time.monotonic()
        if now - self._last_reclaim < self.settings.reclaim_every_seconds:
            return
        self._last_reclaim = now
        try:
            reclaimed = self.store.reclaim_expired_leases()
            if reclaimed:
                logger.info("Returned %d abandoned job(s) to the queue.", reclaimed)
        except Exception:  # noqa: BLE001
            logger.exception("Lease reclamation failed; retrying on the next cycle.")


class EmbeddedQueueWorker:
    """Run a :class:`DurableQueueWorker` on a background thread.

    Used by the local developer profile so `start_phase4.ps1` still launches one
    process. Production runs the worker as its own container instead.
    """

    def __init__(self, store, executor, *, settings: QueueSettings | None = None,
                 concurrency: int = 1):
        self.workers = [
            DurableQueueWorker(store, executor, settings=settings, worker_id=None)
            for _ in range(max(1, min(8, concurrency)))
        ]
        self._threads: list[threading.Thread] = []

    def start(self) -> int:
        for index, worker in enumerate(self.workers):
            thread = threading.Thread(
                target=worker.run_forever, name=f"citeprism-worker-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)
        return len(self._threads)

    def shutdown(self, wait: bool = False) -> None:
        for worker in self.workers:
            worker.request_stop()
        if wait:
            for thread in self._threads:
                thread.join(timeout=10)


__all__ = [
    "DurableQueueWorker",
    "EmbeddedQueueWorker",
    "LeaseRenewal",
    "QueueSettings",
]
