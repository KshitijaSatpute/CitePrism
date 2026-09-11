"""Tenant-scoped SQLite workflow store with append-only human activity.

This database is intentionally separate from the immutable trust repository.
It owns operational state (queues, leases, filters and drafts), while the trust
kernel continues to own evidence provenance.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from src.trust.ids import new_uuid7
from src.workspace.common import (
    WorkspaceConflict,
    WorkspaceNotFound,
    decode,
    encode,
    utc_now,
)
from src.workspace.models import AuditState, CorrectionState, JobState
from src.workspace.sqlite_phase4 import SqliteWorkspacePhase4Mixin


class WorkspaceStore(SqliteWorkspacePhase4Mixin):
    """Small transactional repository used by the API and background workers.

    Phase 4 keeps this SQLite implementation for the single-workstation profile.
    The durable multi-process runtime uses
    :class:`src.workspace.postgres_store.PostgresWorkspaceStore`, which exposes
    the identical surface.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workspace_schema (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspace_manuscripts (
                    tenant_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    legacy_document_id INTEGER,
                    title TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    pdf_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, id),
                    UNIQUE (tenant_id, content_sha256)
                );
                CREATE TABLE IF NOT EXISTS workspace_audits (
                    tenant_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    manuscript_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('queued','processing','completed','completed_with_issues','failed','cancelled')),
                    artifact_path TEXT,
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    PRIMARY KEY (tenant_id, id),
                    UNIQUE (tenant_id, job_id),
                    FOREIGN KEY (tenant_id, manuscript_id)
                      REFERENCES workspace_manuscripts (tenant_id, id)
                );
                CREATE TABLE IF NOT EXISTS workspace_jobs (
                    tenant_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    audit_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','cancel_requested','cancelled')),
                    progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
                    stage TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    error_message TEXT,
                    result_json TEXT,
                    parent_job_id TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    heartbeat_at TEXT,
                    completed_at TEXT,
                    PRIMARY KEY (tenant_id, id),
                    FOREIGN KEY (tenant_id, audit_id)
                      REFERENCES workspace_audits (tenant_id, id)
                );
                CREATE TABLE IF NOT EXISTS workspace_decisions (
                    tenant_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    audit_id TEXT NOT NULL,
                    finding_id TEXT NOT NULL,
                    disposition TEXT NOT NULL CHECK(disposition IN ('confirm','dismiss','escalate','needs_more_evidence')),
                    rationale TEXT NOT NULL,
                    reviewer_id TEXT NOT NULL,
                    supersedes_decision_id TEXT,
                    idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, id),
                    UNIQUE (tenant_id, idempotency_key),
                    UNIQUE (tenant_id, supersedes_decision_id),
                    FOREIGN KEY (tenant_id, audit_id)
                      REFERENCES workspace_audits (tenant_id, id)
                );
                CREATE INDEX IF NOT EXISTS workspace_decision_subject
                  ON workspace_decisions (tenant_id, audit_id, finding_id, created_at);
                CREATE TABLE IF NOT EXISTS workspace_corrections (
                    tenant_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    audit_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('metadata','merge_references','split_reference','reassign_citation')),
                    reference_ids_json TEXT NOT NULL,
                    proposed_value_json TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    reviewer_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('proposed','accepted','rejected','superseded')),
                    supersedes_correction_id TEXT,
                    idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, id),
                    UNIQUE (tenant_id, idempotency_key),
                    UNIQUE (tenant_id, supersedes_correction_id),
                    FOREIGN KEY (tenant_id, audit_id)
                      REFERENCES workspace_audits (tenant_id, id)
                );
                CREATE TABLE IF NOT EXISTS workspace_activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    audit_id TEXT,
                    actor_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS workspace_activity_scope
                  ON workspace_activity (tenant_id, audit_id, id DESC);
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO workspace_schema(version, applied_at) VALUES(1, ?)",
                (utc_now(),),
            )
            # Phase 4 adds durable-queue columns, organizations, assignment,
            # repairs, signed exports and telemetry to the same database.
            self._migrate_phase4(connection)

    @staticmethod
    def _id() -> str:
        return str(new_uuid7())

    @staticmethod
    def _one(row, name: str):
        if row is None:
            raise WorkspaceNotFound(f"{name} was not found")
        return dict(row)

    @staticmethod
    def _activity(
        connection,
        tenant_id: str,
        audit_id: str | None,
        actor_id: str,
        action: str,
        entity_type: str,
        entity_id: str,
        payload: dict,
    ) -> None:
        created_at = utc_now()
        previous = connection.execute(
            "SELECT event_hash FROM workspace_activity WHERE tenant_id=? ORDER BY id DESC LIMIT 1",
            (tenant_id,),
        ).fetchone()
        previous_hash = previous["event_hash"] if previous else "0" * 64
        event = {
            "tenant_id": tenant_id,
            "audit_id": audit_id,
            "actor_id": actor_id,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "payload": payload,
            "previous_hash": previous_hash,
            "created_at": created_at,
        }
        event_hash = hashlib.sha256(encode(event).encode("utf-8")).hexdigest()
        connection.execute(
            """INSERT INTO workspace_activity
               (tenant_id,audit_id,actor_id,action,entity_type,entity_id,payload_json,
                previous_hash,event_hash,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                tenant_id,
                audit_id,
                actor_id,
                action,
                entity_type,
                entity_id,
                encode(payload),
                previous_hash,
                event_hash,
                created_at,
            ),
        )

    def verify_activity_chain(self, tenant_id: str) -> dict:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM workspace_activity WHERE tenant_id=? ORDER BY id", (tenant_id,)
            ).fetchall()
        previous_hash = "0" * 64
        for position, value in enumerate(rows, 1):
            row = dict(value)
            event = {
                "tenant_id": row["tenant_id"],
                "audit_id": row["audit_id"],
                "actor_id": row["actor_id"],
                "action": row["action"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "payload": decode(row["payload_json"], {}),
                "previous_hash": row["previous_hash"],
                "created_at": row["created_at"],
            }
            computed = hashlib.sha256(encode(event).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != computed:
                return {
                    "valid": False,
                    "event_count": len(rows),
                    "failed_at": position,
                    "head_hash": previous_hash,
                }
            previous_hash = row["event_hash"]
        return {
            "valid": True,
            "event_count": len(rows),
            "failed_at": None,
            "head_hash": previous_hash if rows else None,
        }

    def upsert_manuscript(
        self,
        tenant_id: str,
        *,
        title: str,
        original_filename: str,
        content_sha256: str,
        pdf_path: str,
        legacy_document_id: int | None = None,
        actor_id: str = "system",
    ) -> tuple[dict, bool]:
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM workspace_manuscripts WHERE tenant_id=? AND content_sha256=?",
                (tenant_id, content_sha256),
            ).fetchone()
            if existing:
                connection.commit()
                return dict(existing), False
            identifier = self._id()
            connection.execute(
                """INSERT INTO workspace_manuscripts
                   (tenant_id,id,legacy_document_id,title,original_filename,content_sha256,pdf_path,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    tenant_id,
                    identifier,
                    legacy_document_id,
                    title,
                    original_filename,
                    content_sha256,
                    pdf_path,
                    now,
                    now,
                ),
            )
            self._activity(
                connection,
                tenant_id,
                None,
                actor_id,
                "manuscript.created",
                "manuscript",
                identifier,
                {"content_sha256": content_sha256},
            )
            row = connection.execute(
                "SELECT * FROM workspace_manuscripts WHERE tenant_id=? AND id=?",
                (tenant_id, identifier),
            ).fetchone()
            connection.commit()
            return dict(row), True

    def list_manuscripts(self, tenant_id: str) -> list[dict]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT m.*, a.id latest_audit_id, a.state latest_audit_state,
                   COALESCE((SELECT COUNT(*) FROM workspace_decisions d
                     WHERE d.tenant_id=m.tenant_id AND d.audit_id=a.id), 0) decision_count
                   FROM workspace_manuscripts m
                   LEFT JOIN workspace_audits a ON a.tenant_id=m.tenant_id AND a.id=(
                     SELECT a2.id FROM workspace_audits a2
                     WHERE a2.tenant_id=m.tenant_id AND a2.manuscript_id=m.id
                     ORDER BY a2.created_at DESC LIMIT 1)
                   WHERE m.tenant_id=? ORDER BY m.updated_at DESC""",
                (tenant_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_manuscript(self, tenant_id: str, manuscript_id: str) -> dict:
        with self.connection() as connection:
            return self._one(
                connection.execute(
                    "SELECT * FROM workspace_manuscripts WHERE tenant_id=? AND id=?",
                    (tenant_id, manuscript_id),
                ).fetchone(),
                "manuscript",
            )

    def update_manuscript_title(self, tenant_id: str, manuscript_id: str, title: str) -> None:
        title = title.strip()[:1000]
        if not title:
            return
        with self.connection() as connection:
            connection.execute(
                "UPDATE workspace_manuscripts SET title=?,updated_at=? WHERE tenant_id=? AND id=?",
                (title, utc_now(), tenant_id, manuscript_id),
            )

    def create_audit_job(
        self,
        tenant_id: str,
        manuscript_id: str,
        request: dict,
        actor_id: str,
        *,
        kind: str = "audit",
        priority: int = 100,
        max_attempts: int = 3,
    ) -> tuple[dict, dict]:
        now, audit_id, job_id = utc_now(), self._id(), self._id()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._one(
                connection.execute(
                    "SELECT id FROM workspace_manuscripts WHERE tenant_id=? AND id=?",
                    (tenant_id, manuscript_id),
                ).fetchone(),
                "manuscript",
            )
            connection.execute(
                """INSERT INTO workspace_audits
                   (tenant_id,id,manuscript_id,job_id,state,summary_json,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (tenant_id, audit_id, manuscript_id, job_id, AuditState.QUEUED.value, "{}", now),
            )
            connection.execute(
                """INSERT INTO workspace_jobs
                   (tenant_id,id,audit_id,state,progress,stage,request_json,parent_job_id,
                    created_at,kind,priority,max_attempts,available_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    tenant_id,
                    job_id,
                    audit_id,
                    JobState.QUEUED.value,
                    0,
                    "queued",
                    encode(request),
                    request.get("retry_of_job_id"),
                    now,
                    kind,
                    max(0, min(1000, int(priority))),
                    max(1, min(10, int(max_attempts))),
                    now,
                ),
            )
            self._activity(
                connection,
                tenant_id,
                audit_id,
                actor_id,
                "audit.queued",
                "audit",
                audit_id,
                {"job_id": job_id},
            )
            audit = dict(
                connection.execute(
                    "SELECT * FROM workspace_audits WHERE tenant_id=? AND id=?",
                    (tenant_id, audit_id),
                ).fetchone()
            )
            job = dict(
                connection.execute(
                    "SELECT * FROM workspace_jobs WHERE tenant_id=? AND id=?", (tenant_id, job_id)
                ).fetchone()
            )
            connection.commit()
            return audit, job

    def get_audit(self, tenant_id: str, audit_id: str) -> dict:
        with self.connection() as connection:
            return self._one(
                connection.execute(
                    "SELECT * FROM workspace_audits WHERE tenant_id=? AND id=?",
                    (tenant_id, audit_id),
                ).fetchone(),
                "audit",
            )

    def list_audits(self, tenant_id: str, manuscript_id: str) -> list[dict]:
        self.get_manuscript(tenant_id, manuscript_id)
        with self.connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM workspace_audits WHERE tenant_id=? AND manuscript_id=?
                   ORDER BY created_at DESC""",
                    (tenant_id, manuscript_id),
                ).fetchall()
            ]

    def get_job(self, tenant_id: str, job_id: str) -> dict:
        with self.connection() as connection:
            return self._one(
                connection.execute(
                    "SELECT * FROM workspace_jobs WHERE tenant_id=? AND id=?", (tenant_id, job_id)
                ).fetchone(),
                "job",
            )

    def recover_interrupted_jobs(self) -> int:
        """Return abandoned process-local work to the queue after a restart."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT tenant_id,id,audit_id FROM workspace_jobs WHERE state IN (?,?)",
                (JobState.RUNNING.value, JobState.CANCEL_REQUESTED.value),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """UPDATE workspace_jobs SET state=?,stage=?,progress=0,lease_owner=NULL,
                       lease_expires_at=NULL,heartbeat_at=NULL WHERE tenant_id=? AND id=?""",
                    (JobState.QUEUED.value, "recovered_after_restart", row["tenant_id"], row["id"]),
                )
                connection.execute(
                    "UPDATE workspace_audits SET state=? WHERE tenant_id=? AND id=?",
                    (AuditState.QUEUED.value, row["tenant_id"], row["audit_id"]),
                )
            connection.commit()
            return len(rows)

    def list_queued_jobs(self) -> list[dict]:
        with self.connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM workspace_jobs WHERE state=? AND available_at<=?
                       ORDER BY priority, available_at, created_at""",
                    (JobState.QUEUED.value, utc_now()),
                ).fetchall()
            ]

    def claim_job(self, tenant_id: str, job_id: str, worker_id: str) -> dict | None:
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE workspace_jobs SET state=?,stage=?,progress=1,attempts=attempts+1,
                   lease_owner=?,started_at=COALESCE(started_at,?),heartbeat_at=?
                   WHERE tenant_id=? AND id=? AND state=?""",
                (
                    JobState.RUNNING.value,
                    "starting",
                    worker_id,
                    now,
                    now,
                    tenant_id,
                    job_id,
                    JobState.QUEUED.value,
                ),
            ).rowcount
            if not changed:
                connection.rollback()
                return None
            row = dict(
                connection.execute(
                    "SELECT * FROM workspace_jobs WHERE tenant_id=? AND id=?", (tenant_id, job_id)
                ).fetchone()
            )
            connection.execute(
                "UPDATE workspace_audits SET state=?,started_at=COALESCE(started_at,?) WHERE tenant_id=? AND id=?",
                (AuditState.PROCESSING.value, now, tenant_id, row["audit_id"]),
            )
            self._activity(
                connection,
                tenant_id,
                row["audit_id"],
                worker_id,
                "audit.started",
                "job",
                job_id,
                {"attempt": row["attempts"]},
            )
            connection.commit()
            return row

    def update_job_progress(self, tenant_id: str, job_id: str, progress: int, stage: str) -> bool:
        progress = max(1, min(99, int(progress)))
        with self.connection() as connection:
            changed = connection.execute(
                """UPDATE workspace_jobs SET progress=?,stage=?,heartbeat_at=?
                   WHERE tenant_id=? AND id=? AND state=?""",
                (progress, stage[:200], utc_now(), tenant_id, job_id, JobState.RUNNING.value),
            ).rowcount
            return bool(changed)

    def job_cancel_requested(self, tenant_id: str, job_id: str) -> bool:
        row = self.get_job(tenant_id, job_id)
        return bool(row["cancel_requested"])

    def request_cancel(self, tenant_id: str, job_id: str, actor_id: str) -> dict:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._one(
                connection.execute(
                    "SELECT * FROM workspace_jobs WHERE tenant_id=? AND id=?", (tenant_id, job_id)
                ).fetchone(),
                "job",
            )
            if row["state"] in {
                JobState.SUCCEEDED.value,
                JobState.FAILED.value,
                JobState.CANCELLED.value,
            }:
                raise WorkspaceConflict("completed jobs cannot be cancelled")
            next_state = (
                JobState.CANCELLED.value
                if row["state"] == JobState.QUEUED.value
                else JobState.CANCEL_REQUESTED.value
            )
            completed = utc_now() if next_state == JobState.CANCELLED.value else None
            connection.execute(
                """UPDATE workspace_jobs SET state=?,cancel_requested=1,completed_at=COALESCE(?,completed_at)
                   WHERE tenant_id=? AND id=?""",
                (next_state, completed, tenant_id, job_id),
            )
            if next_state == JobState.CANCELLED.value:
                connection.execute(
                    "UPDATE workspace_audits SET state=?,completed_at=? WHERE tenant_id=? AND id=?",
                    (AuditState.CANCELLED.value, completed, tenant_id, row["audit_id"]),
                )
            self._activity(
                connection,
                tenant_id,
                row["audit_id"],
                actor_id,
                "audit.cancel_requested",
                "job",
                job_id,
                {},
            )
            result = dict(
                connection.execute(
                    "SELECT * FROM workspace_jobs WHERE tenant_id=? AND id=?", (tenant_id, job_id)
                ).fetchone()
            )
            connection.commit()
            return result

    def finish_job(
        self,
        tenant_id: str,
        job_id: str,
        *,
        success: bool,
        artifact_path: str | None = None,
        artifact_key: str | None = None,
        summary: dict | None = None,
        result: dict | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        completed = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._one(
                connection.execute(
                    "SELECT * FROM workspace_jobs WHERE tenant_id=? AND id=?", (tenant_id, job_id)
                ).fetchone(),
                "job",
            )
            cancelled = bool(row["cancel_requested"])
            job_state = (
                JobState.CANCELLED.value
                if cancelled
                else JobState.SUCCEEDED.value
                if success
                else JobState.FAILED.value
            )
            audit_state = (
                AuditState.CANCELLED.value
                if cancelled
                else (
                    AuditState.COMPLETED_WITH_ISSUES.value
                    if success and (summary or {}).get("references_to_review", 0)
                    else AuditState.COMPLETED.value
                )
                if success
                else AuditState.FAILED.value
            )
            connection.execute(
                """UPDATE workspace_jobs SET state=?,progress=?,stage=?,error_code=?,error_message=?,
                   result_json=?,completed_at=?,heartbeat_at=?,lease_owner=NULL,
                   lease_expires_at=NULL WHERE tenant_id=? AND id=?""",
                (
                    job_state,
                    100 if success and not cancelled else row["progress"],
                    "completed" if success else "cancelled" if cancelled else "failed",
                    error_code,
                    error_message,
                    encode(result or {}),
                    completed,
                    completed,
                    tenant_id,
                    job_id,
                ),
            )
            connection.execute(
                """UPDATE workspace_audits SET state=?,artifact_path=?,artifact_key=?,
                   summary_json=?,error_code=?,error_message=?,completed_at=?
                   WHERE tenant_id=? AND id=?""",
                (
                    audit_state,
                    artifact_path,
                    artifact_key,
                    encode(summary or {}),
                    error_code,
                    error_message,
                    completed,
                    tenant_id,
                    row["audit_id"],
                ),
            )
            action = (
                "audit.cancelled" if cancelled else "audit.completed" if success else "audit.failed"
            )
            self._activity(
                connection,
                tenant_id,
                row["audit_id"],
                row["lease_owner"] or "worker",
                action,
                "audit",
                row["audit_id"],
                {"error_code": error_code},
            )
            connection.commit()

    def retry_job(self, tenant_id: str, job_id: str, actor_id: str) -> tuple[dict, dict]:
        previous = self.get_job(tenant_id, job_id)
        if previous["state"] not in {
            JobState.FAILED.value,
            JobState.CANCELLED.value,
            "dead_letter",
        }:
            raise WorkspaceConflict("only failed, cancelled or dead-lettered jobs can be retried")
        audit = self.get_audit(tenant_id, previous["audit_id"])
        manuscript_id = audit["manuscript_id"]
        request = decode(previous["request_json"], {})
        request["retry_of_job_id"] = job_id
        return self.create_audit_job(tenant_id, manuscript_id, request, actor_id)

    def latest_decisions(self, tenant_id: str, audit_id: str) -> dict[str, dict]:
        self.get_audit(tenant_id, audit_id)
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT d.* FROM workspace_decisions d WHERE d.tenant_id=? AND d.audit_id=?
                   AND NOT EXISTS (SELECT 1 FROM workspace_decisions child
                     WHERE child.tenant_id=d.tenant_id AND child.supersedes_decision_id=d.id)
                   ORDER BY d.created_at""",
                (tenant_id, audit_id),
            ).fetchall()
            return {row["finding_id"]: dict(row) for row in rows}

    def record_decision(
        self,
        tenant_id: str,
        audit_id: str,
        finding_id: str,
        disposition: str,
        rationale: str,
        reviewer_id: str,
        idempotency_key: str,
        expected_current_id: str | None = None,
    ) -> dict:
        now, identifier = utc_now(), self._id()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._one(
                connection.execute(
                    "SELECT id FROM workspace_audits WHERE tenant_id=? AND id=?",
                    (tenant_id, audit_id),
                ).fetchone(),
                "audit",
            )
            replay = connection.execute(
                "SELECT * FROM workspace_decisions WHERE tenant_id=? AND idempotency_key=?",
                (tenant_id, idempotency_key),
            ).fetchone()
            if replay:
                connection.commit()
                return dict(replay)
            current = connection.execute(
                """SELECT d.* FROM workspace_decisions d
                   WHERE d.tenant_id=? AND d.audit_id=? AND d.finding_id=?
                   AND NOT EXISTS (SELECT 1 FROM workspace_decisions child
                     WHERE child.tenant_id=d.tenant_id AND child.supersedes_decision_id=d.id)
                   ORDER BY d.created_at DESC LIMIT 1""",
                (tenant_id, audit_id, finding_id),
            ).fetchone()
            current_id = current["id"] if current else None
            if current_id != expected_current_id:
                raise WorkspaceConflict("the finding decision changed; refresh before saving")
            connection.execute(
                """INSERT INTO workspace_decisions
                   (tenant_id,id,audit_id,finding_id,disposition,rationale,reviewer_id,
                    supersedes_decision_id,idempotency_key,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    tenant_id,
                    identifier,
                    audit_id,
                    finding_id,
                    disposition,
                    rationale,
                    reviewer_id,
                    current_id,
                    idempotency_key,
                    now,
                ),
            )
            self._activity(
                connection,
                tenant_id,
                audit_id,
                reviewer_id,
                "decision.recorded",
                "finding",
                finding_id,
                {
                    "decision_id": identifier,
                    "disposition": disposition,
                    "supersedes_decision_id": current_id,
                },
            )
            row = dict(
                connection.execute(
                    "SELECT * FROM workspace_decisions WHERE tenant_id=? AND id=?",
                    (tenant_id, identifier),
                ).fetchone()
            )
            connection.commit()
            return row

    def record_correction(
        self,
        tenant_id: str,
        audit_id: str,
        *,
        kind: str,
        reference_ids: list[str],
        proposed_value: dict,
        rationale: str,
        reviewer_id: str,
        idempotency_key: str,
        supersedes_id: str | None = None,
    ) -> dict:
        now, identifier = utc_now(), self._id()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._one(
                connection.execute(
                    "SELECT id FROM workspace_audits WHERE tenant_id=? AND id=?",
                    (tenant_id, audit_id),
                ).fetchone(),
                "audit",
            )
            replay = connection.execute(
                "SELECT * FROM workspace_corrections WHERE tenant_id=? AND idempotency_key=?",
                (tenant_id, idempotency_key),
            ).fetchone()
            if replay:
                connection.commit()
                return dict(replay)
            if supersedes_id:
                parent = self._one(
                    connection.execute(
                        "SELECT * FROM workspace_corrections WHERE tenant_id=? AND id=?",
                        (tenant_id, supersedes_id),
                    ).fetchone(),
                    "correction",
                )
                if parent["audit_id"] != audit_id:
                    raise WorkspaceConflict("correction supersession must stay in one audit")
                connection.execute(
                    "UPDATE workspace_corrections SET state=? WHERE tenant_id=? AND id=?",
                    (CorrectionState.SUPERSEDED.value, tenant_id, supersedes_id),
                )
            connection.execute(
                """INSERT INTO workspace_corrections
                   (tenant_id,id,audit_id,kind,reference_ids_json,proposed_value_json,rationale,
                    reviewer_id,state,supersedes_correction_id,idempotency_key,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    tenant_id,
                    identifier,
                    audit_id,
                    kind,
                    encode(reference_ids),
                    encode(proposed_value),
                    rationale,
                    reviewer_id,
                    CorrectionState.PROPOSED.value,
                    supersedes_id,
                    idempotency_key,
                    now,
                ),
            )
            self._activity(
                connection,
                tenant_id,
                audit_id,
                reviewer_id,
                "correction.proposed",
                "correction",
                identifier,
                {"kind": kind, "reference_ids": reference_ids},
            )
            row = dict(
                connection.execute(
                    "SELECT * FROM workspace_corrections WHERE tenant_id=? AND id=?",
                    (tenant_id, identifier),
                ).fetchone()
            )
            connection.commit()
            return row

    def transition_correction(
        self, tenant_id: str, correction_id: str, state: str, rationale: str, actor_id: str
    ) -> dict:
        if state not in {CorrectionState.ACCEPTED.value, CorrectionState.REJECTED.value}:
            raise ValueError("invalid correction transition")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._one(
                connection.execute(
                    "SELECT * FROM workspace_corrections WHERE tenant_id=? AND id=?",
                    (tenant_id, correction_id),
                ).fetchone(),
                "correction",
            )
            if row["state"] != CorrectionState.PROPOSED.value:
                raise WorkspaceConflict("only proposed corrections can be decided")
            connection.execute(
                "UPDATE workspace_corrections SET state=? WHERE tenant_id=? AND id=?",
                (state, tenant_id, correction_id),
            )
            self._activity(
                connection,
                tenant_id,
                row["audit_id"],
                actor_id,
                f"correction.{state}",
                "correction",
                correction_id,
                {"rationale": rationale},
            )
            result = dict(
                connection.execute(
                    "SELECT * FROM workspace_corrections WHERE tenant_id=? AND id=?",
                    (tenant_id, correction_id),
                ).fetchone()
            )
            connection.commit()
            return result

    def list_corrections(self, tenant_id: str, audit_id: str) -> list[dict]:
        self.get_audit(tenant_id, audit_id)
        with self.connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM workspace_corrections WHERE tenant_id=? AND audit_id=?
                   ORDER BY created_at""",
                    (tenant_id, audit_id),
                ).fetchall()
            ]

    def list_activity(
        self, tenant_id: str, audit_id: str | None = None, limit: int = 100
    ) -> list[dict]:
        limit = max(1, min(500, int(limit)))
        with self.connection() as connection:
            if audit_id:
                rows = connection.execute(
                    """SELECT * FROM workspace_activity WHERE tenant_id=? AND audit_id=?
                       ORDER BY id DESC LIMIT ?""",
                    (tenant_id, audit_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM workspace_activity WHERE tenant_id=?
                       ORDER BY id DESC LIMIT ?""",
                    (tenant_id, limit),
                ).fetchall()
            return [dict(row) for row in rows]

    def list_review_cases(
        self,
        tenant_id: str,
        *,
        priority: str | None = None,
        status: str | None = None,
        query: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """Review artifacts are projected in the service; this returns candidate audits."""
        del priority, status, query
        with self.connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT a.*,m.title manuscript_title FROM workspace_audits a
                   JOIN workspace_manuscripts m ON m.tenant_id=a.tenant_id AND m.id=a.manuscript_id
                   WHERE a.tenant_id=? AND a.artifact_path IS NOT NULL
                   ORDER BY a.completed_at DESC LIMIT ?""",
                    (tenant_id, min(max(limit, 1), 500)),
                ).fetchall()
            ]


def hydrate_manuscript(row: dict) -> dict:
    return {
        "id": row["id"],
        "legacy_document_id": row.get("legacy_document_id"),
        "title": row["title"],
        "original_filename": row["original_filename"],
        "content_sha256": row["content_sha256"],
        "pdf_path": row["pdf_path"],
        "object_backend": row.get("object_backend") or "local",
        "object_key": row.get("object_key"),
        "text_layer_status": row.get("text_layer_status") or "unknown",
        "latest_audit_id": row.get("latest_audit_id"),
        "latest_audit_state": row.get("latest_audit_state"),
        "open_findings": 0,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def hydrate_audit(row: dict) -> dict:
    return {
        "id": row["id"],
        "manuscript_id": row["manuscript_id"],
        "job_id": row["job_id"],
        "state": row["state"],
        "artifact_path": row.get("artifact_path"),
        "artifact_key": row.get("artifact_key"),
        "summary": decode(row.get("summary_json"), {}),
        "error_code": row.get("error_code"),
        "error_message": row.get("error_message"),
        "created_at": row["created_at"],
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
    }


def hydrate_job(row: dict) -> dict:
    return {
        "id": row["id"],
        "audit_id": row["audit_id"],
        "state": row["state"],
        "progress": row["progress"],
        "stage": row["stage"],
        "attempts": row["attempts"],
        "cancel_requested": bool(row.get("cancel_requested")),
        "error_code": row.get("error_code"),
        "error_message": row.get("error_message"),
        "created_at": row["created_at"],
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
    }


def hydrate_decision(row: dict) -> dict:
    return {
        key: row.get(key)
        for key in (
            "id",
            "audit_id",
            "finding_id",
            "disposition",
            "rationale",
            "reviewer_id",
            "supersedes_decision_id",
            "created_at",
        )
    }


def hydrate_correction(row: dict) -> dict:
    return {
        "id": row["id"],
        "audit_id": row["audit_id"],
        "kind": row["kind"],
        "reference_ids": decode(row.get("reference_ids_json"), []),
        "proposed_value": decode(row.get("proposed_value_json"), {}),
        "rationale": row["rationale"],
        "reviewer_id": row["reviewer_id"],
        "state": row["state"],
        "supersedes_correction_id": row.get("supersedes_correction_id"),
        "created_at": row["created_at"],
    }


def hydrate_activity(rows: Iterable[dict]) -> list[dict]:
    return [
        {
            "id": row["id"],
            "audit_id": row.get("audit_id"),
            "actor_id": row["actor_id"],
            "action": row["action"],
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "payload": decode(row.get("payload_json"), {}),
            "previous_hash": row["previous_hash"],
            "event_hash": row["event_hash"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


__all__ = [
    "WorkspaceConflict",
    "WorkspaceNotFound",
    "WorkspaceStore",
    "decode",
    "encode",
    "hydrate_activity",
    "hydrate_audit",
    "hydrate_correction",
    "hydrate_decision",
    "hydrate_job",
    "hydrate_manuscript",
    "utc_now",
]
