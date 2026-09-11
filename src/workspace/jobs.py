"""In-process audit execution for the single-workstation profile.

Phase 3 shipped this as the only execution path. Phase 4 keeps it — running one
process is still the right answer on a laptop — but it now delegates the actual
work to :class:`src.workspace.executor.AuditExecutor`, the same code the durable
worker runs. There is one execution path and two ways to schedule it.

Set `CITEPRISM_RUN_EMBEDDED_WORKER=false` and run `scripts/worker.py` separately
to move to the durable multi-process runtime.
"""

from __future__ import annotations

import logging
import socket
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock

from src.workspace.executor import AuditExecutor, JobCancelled, JobProgress

logger = logging.getLogger(__name__)

__all__ = ["AuditJobRunner", "JobCancelled", "JobProgress"]


class AuditJobRunner:
    """Bounded thread pool that claims and runs queued audits in this process."""

    def __init__(
        self,
        store,
        pipeline,
        *,
        max_workers: int = 2,
        worker_id: str | None = None,
        config=None,
        pricing=None,
        object_store=None,
        repair_service=None,
        queue_settings=None,
    ):
        self.store = store
        self.pipeline = pipeline
        self.worker_id = worker_id or f"{socket.gethostname()}:{id(self)}"
        self.executor = ThreadPoolExecutor(
            max_workers=max(1, min(max_workers, 8)), thread_name_prefix="citeprism-audit"
        )
        self.audit_executor = AuditExecutor(
            store,
            pipeline,
            config=config,
            pricing=pricing,
            queue_settings=queue_settings,
            repair_service=repair_service,
        )
        if object_store is not None:
            self.audit_executor.object_store = object_store
        self._lock = Lock()
        self._futures: dict[str, Future] = {}

    def start(self) -> int:
        recovered = self.store.recover_interrupted_jobs()
        for row in self.store.list_queued_jobs():
            self.submit(row["tenant_id"], row["id"])
        return recovered

    def submit(self, tenant_id: str, job_id: str) -> None:
        with self._lock:
            existing = self._futures.get(job_id)
            if existing and not existing.done():
                return
            future = self.executor.submit(self._run, tenant_id, job_id)
            self._futures[job_id] = future
            future.add_done_callback(lambda _future: self._forget(job_id))

    def _forget(self, job_id: str) -> None:
        with self._lock:
            self._futures.pop(job_id, None)

    def _run(self, tenant_id: str, job_id: str) -> None:
        job = self.store.claim_job(tenant_id, job_id, self.worker_id)
        if job is None:
            # Another worker already holds this job. That is expected, not an error.
            return
        self.audit_executor.execute(tenant_id, dict(job))

    def set_repair_service(self, repair_service) -> None:
        self.audit_executor.repair_service = repair_service

    def shutdown(self, wait: bool = True) -> None:
        self.executor.shutdown(wait=wait, cancel_futures=False)
