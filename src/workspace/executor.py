"""Execute one leased audit job.

Phase 3 put this logic inside `AuditJobRunner`, which also owned the thread
pool. Phase 4 separates them: the queue owns *when* a job runs, this owns *what*
running it means. That is what lets the same execution path serve the in-process
developer profile and a fleet of worker containers.

Failure handling distinguishes two cases that Phase 3 treated alike. A transient
provider outage is released back to the queue with backoff, because retrying is
likely to work. A deterministic failure — an encrypted PDF, a malformed
artifact — fails immediately, because retrying it three times only wastes the
reviewer's time and the provider budget.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from src.evidence.review import build_review
from src.telemetry.recorder import TelemetryRecorder
from src.workspace.common import decode
from src.workspace.service import _safe_error

logger = logging.getLogger(__name__)

# Failures worth another attempt on a healthy worker.
RETRYABLE_CODES = frozenset(
    {
        "service_unavailable",
        "rate_limit",
        "timeout",
        "provider_error",
        "connectionerror",
        "readtimeout",
        "oserror",
        "ioerror",
    }
)


class JobCancelled(RuntimeError):
    """The operator asked for this audit to stop."""


class JobProgress:
    """Progress sink that persists only bounded, public stage text."""

    def __init__(self, store, tenant_id: str, job_id: str, recorder=None):
        self.store = store
        self.tenant_id = tenant_id
        self.job_id = job_id
        self.recorder = recorder

    def progress(self, value, text: str = "Processing") -> None:
        if self.store.job_cancel_requested(self.tenant_id, self.job_id):
            raise JobCancelled("audit cancellation was requested")
        number = float(value)
        percent = round(number * 100) if number <= 1 else round(number)
        self.store.update_job_progress(self.tenant_id, self.job_id, percent, str(text)[:200])

    # The Phase 2 provider recorder calls this to report a retry.
    def __call__(self, message: str) -> None:  # pragma: no cover - display only
        try:
            self.progress(0.5, message)
        except JobCancelled:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("Progress update failed; the audit continues.")


class AuditExecutor:
    """Run the evidence pipeline for one job and record the outcome."""

    def __init__(self, store, pipeline, *, config=None, pricing=None, queue_settings=None,
                 repair_service=None):
        self.store = store
        self.pipeline = pipeline
        self.config = config
        self.pricing = pricing
        self.queue_settings = queue_settings
        self.repair_service = repair_service

    def execute(self, tenant_id: str, job: dict) -> None:
        job_id = str(job["id"])
        request = decode(job.get("request_json"), {})
        options = request.get("options") or {}
        worker_id = str(job.get("lease_owner") or "worker")
        audit = self.store.get_audit(tenant_id, job["audit_id"])

        recorder = TelemetryRecorder(
            self.store,
            tenant_id=tenant_id,
            audit_id=str(job["audit_id"]),
            job_id=job_id,
            pricing=self.pricing,
            enabled=bool(getattr(self.config, "ENABLE_TELEMETRY", True)),
        )

        try:
            if self.store.job_cancel_requested(tenant_id, job_id):
                self.store.finish_job(tenant_id, job_id, success=False)
                return

            with recorder.stage("audit_total"):
                result = self.pipeline.process_document(
                    Path(request["pdf_path"]),
                    progress_bar=JobProgress(self.store, tenant_id, job_id, recorder),
                    force_parse=bool(options.get("force_parse")),
                    force_enrich=bool(options.get("force_enrich")),
                    force_score=bool(options.get("force_score")),
                    run_missing_citations=bool(options.get("run_missing_citations")),
                    original_filename=request.get("original_filename"),
                )

            if not result.get("success"):
                message = "; ".join(str(item) for item in result.get("errors", []))
                raise RuntimeError(message or "the evidence pipeline did not complete")

            artifact_path = str((result.get("file_paths") or {}).get("scored") or "")
            if not artifact_path:
                raise RuntimeError("the completed pipeline did not publish an evidence artifact")

            artifact = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
            summary = build_review(artifact)["counts"]

            self._record_telemetry(recorder, result, artifact, summary)
            artifact_key = self._publish_artifact(tenant_id, artifact_path, artifact)
            self._update_manuscript(tenant_id, audit, artifact, result)

            self.store.finish_job(
                tenant_id,
                job_id,
                success=True,
                artifact_path=artifact_path,
                artifact_key=artifact_key,
                summary=summary,
                result={
                    key: value
                    for key, value in result.items()
                    if key in {"timings", "stages_completed", "stages_skipped", "file_paths"}
                },
            )
            self._reconcile_repairs(tenant_id, str(job["audit_id"]))

        except JobCancelled:
            logger.info("Audit job %s was cancelled by an operator.", job_id)
            self.store.finish_job(tenant_id, job_id, success=False)
        except Exception as error:  # noqa: BLE001 - one job must not kill the worker
            code, message = _safe_error(error)
            logger.exception("Audit job %s failed with %s", job_id, code)
            self._handle_failure(tenant_id, job_id, worker_id, job, code, message)
            self._reconcile_repairs(tenant_id, str(job["audit_id"]))
        finally:
            recorder.flush()

    # -- helpers -------------------------------------------------------
    def _handle_failure(self, tenant_id, job_id, worker_id, job, code, message) -> None:
        retryable = str(code).lower() in RETRYABLE_CODES
        attempts = int(job.get("attempts") or 1)
        budget = int(job.get("max_attempts") or 3)
        if retryable and attempts < budget and hasattr(self.store, "release_job"):
            backoff = (
                self.queue_settings.backoff_for(attempts)
                if self.queue_settings is not None
                else 15.0 * attempts
            )
            outcome = self.store.release_job(
                tenant_id,
                job_id,
                worker_id,
                error_code=code,
                error_message=message,
                backoff_seconds=backoff,
            )
            logger.info(
                "Job %s %s after a %s failure (attempt %d/%d).",
                job_id, outcome, code, attempts, budget,
            )
            return
        self.store.finish_job(
            tenant_id, job_id, success=False, error_code=code, error_message=message
        )

    def _record_telemetry(self, recorder, result, artifact, summary) -> None:
        for stage, seconds in (result.get("timings") or {}).items():
            try:
                recorder.record_stage(str(stage).replace("_sec", ""), float(seconds))
            except (TypeError, ValueError):
                continue
        recorder.record_provider_calls(artifact.get("provider_runs") or [])
        recorder.add(_quality_event(summary))

    def _publish_artifact(self, tenant_id: str, artifact_path: str, artifact: dict) -> str | None:
        """Copy the evidence artifact into object storage when one is configured."""
        object_store = getattr(self, "object_store", None)
        if object_store is None:
            return None
        try:
            from src.workspace.objects import content_key, digest_bytes

            payload = Path(artifact_path).read_bytes()
            digest = digest_bytes(payload)
            key = content_key(tenant_id, "artifacts", digest, suffix=".json")
            reference = object_store.put_bytes(payload, key=key, media_type="application/json")
            return reference.key
        except Exception:  # noqa: BLE001 - object publication is not load bearing
            logger.exception("Could not publish the evidence artifact to object storage.")
            return None

    def _update_manuscript(self, tenant_id: str, audit: dict, artifact: dict, result: dict) -> None:
        title = (
            artifact.get("manuscript_metadata") or artifact.get("metadata") or {}
        ).get("title")
        if title:
            self.store.update_manuscript_title(
                tenant_id, audit["manuscript_id"], str(title)
            )
        status = ((result.get("text_layer") or {}).get("text_layer_status")) or (
            (artifact.get("source_document") or {}).get("ocr") and "ocr_applied"
        )
        if status and hasattr(self.store, "update_manuscript_text_layer"):
            try:
                self.store.update_manuscript_text_layer(
                    tenant_id, audit["manuscript_id"], str(status)
                )
            except Exception:  # noqa: BLE001
                logger.debug("Could not record the manuscript text-layer status.")

    def _reconcile_repairs(self, tenant_id: str, audit_id: str) -> None:
        if self.repair_service is None:
            return
        try:
            self.repair_service.reconcile(tenant_id, audit_id)
        except Exception:  # noqa: BLE001
            logger.exception("Repair reconciliation failed; records stay queued.")


def _quality_event(summary: dict):
    from src.telemetry.recorder import TelemetryEvent

    return TelemetryEvent(
        stage="audit_quality",
        attributes={
            "total_references": summary.get("total_references", 0),
            "resolved_references": summary.get("resolved_references", 0),
            "system_issues": summary.get("system_issues", 0),
            "findings": summary.get("findings", 0),
        },
    )


__all__ = ["AuditExecutor", "JobCancelled", "JobProgress", "RETRYABLE_CODES"]
