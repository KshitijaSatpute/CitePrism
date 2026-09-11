"""PostgreSQL workspace store: durable queue, organizations, telemetry, exports.

This is the Phase 4 replacement for the process-local SQLite workspace. It keeps
the exact row shape produced by :mod:`src.workspace.store` so the existing
``hydrate_*`` helpers, the application service and the API are unchanged.

Three properties matter and are enforced here rather than by convention:

* **Single winner claims.** ``lease_next_job`` uses ``FOR UPDATE SKIP LOCKED``
  so N workers on N machines never run the same audit twice.
* **Crash safety.** A worker that dies stops heart-beating; its lease expires
  and ``reclaim_expired_leases`` returns the job to the queue with its attempt
  count intact, so poison jobs eventually reach the dead-letter state instead of
  looping forever.
* **Tenant isolation.** Every statement carries the tenant key. A cross-tenant
  identifier resolves to "not found", never to another organization's row.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from src.trust.ids import new_uuid7
from src.trust.ports import TrustConfigurationError
from src.workspace.models import AuditState, CorrectionState, JobState
from src.workspace.common import (
    WorkspaceConflict,
    WorkspaceNotFound,
    encode,
    utc_now,
)

logger = logging.getLogger(__name__)
SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
ZERO_HASH = "0" * 64

TERMINAL_JOB_STATES = (
    JobState.SUCCEEDED.value,
    JobState.FAILED.value,
    JobState.CANCELLED.value,
    "dead_letter",
)


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value


def _json_text(value: Any, fallback: str = "{}") -> str:
    if value is None:
        return fallback
    if isinstance(value, str):
        return value
    return encode(value)


class PostgresWorkspaceStore:
    """Durable workspace repository backed by PostgreSQL 14+."""

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "citeprism_workspace",
        min_size: int = 1,
        max_size: int = 8,
        connect_timeout: int = 10,
        statement_timeout_ms: int = 30_000,
    ):
        if not dsn or not dsn.startswith(("postgresql://", "postgres://")):
            raise TrustConfigurationError("a PostgreSQL workspace DSN is required")
        if not SCHEMA_NAME.fullmatch(schema):
            raise TrustConfigurationError("workspace schema name is not a safe identifier")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise TrustConfigurationError(
                "install psycopg to use the PostgreSQL workspace backend"
            ) from exc
        self._psycopg = psycopg
        self._dict_row = dict_row
        self.dsn = dsn
        self.schema = schema
        self.statement_timeout_ms = int(statement_timeout_ms)
        self._pool = None
        try:
            from psycopg_pool import ConnectionPool

            self._pool = ConnectionPool(
                dsn,
                min_size=max(1, min_size),
                max_size=max(min_size, max_size),
                kwargs={"connect_timeout": connect_timeout, "row_factory": dict_row},
                open=True,
                name="citeprism-workspace",
            )
        except ImportError:  # pragma: no cover - pooling is optional
            logger.info("psycopg_pool is not installed; using per-call connections.")
        self._connect_timeout = connect_timeout

    # ------------------------------------------------------------------
    # connection plumbing
    # ------------------------------------------------------------------
    @contextmanager
    def connection(self):
        if self._pool is not None:
            with self._pool.connection() as connection:
                self._prepare(connection)
                yield connection
        else:
            with self._psycopg.connect(
                self.dsn, connect_timeout=self._connect_timeout, row_factory=self._dict_row
            ) as connection:
                self._prepare(connection)
                yield connection

    def _prepare(self, connection) -> None:
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{self.schema}", pg_catalog')
            cursor.execute(f"SET statement_timeout = {self.statement_timeout_ms}")

    @contextmanager
    def _cursor(self):
        with self.connection() as connection:
            with connection.cursor() as cursor:
                yield cursor
            connection.commit()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _id() -> str:
        return str(new_uuid7())

    @staticmethod
    def _one(row, name: str) -> dict:
        if row is None:
            raise WorkspaceNotFound(f"{name} was not found")
        return dict(row)

    @staticmethod
    def _row(row: dict | None) -> dict | None:
        if row is None:
            return None
        return {key: _iso(value) for key, value in dict(row).items()}

    @classmethod
    def _rows(cls, rows: Iterable[dict]) -> list[dict]:
        return [cls._row(row) for row in rows]  # type: ignore[misc]

    @staticmethod
    def _manuscript_row(row: dict | None) -> dict | None:
        """Present a manuscript with Phase 3 key names."""
        if row is None:
            return None
        value = {key: _iso(item) for key, item in dict(row).items()}
        value["id"] = str(value["id"])
        return value

    @classmethod
    def _audit_row(cls, row: dict | None) -> dict | None:
        if row is None:
            return None
        value = {key: _iso(item) for key, item in dict(row).items()}
        value["id"] = str(value["id"])
        value["manuscript_id"] = str(value["manuscript_id"])
        value["job_id"] = str(value["job_id"])
        value["summary_json"] = _json_text(value.pop("summary", None))
        return value

    @classmethod
    def _job_row(cls, row: dict | None) -> dict | None:
        if row is None:
            return None
        value = {key: _iso(item) for key, item in dict(row).items()}
        value["id"] = str(value["id"])
        value["audit_id"] = str(value["audit_id"])
        value["tenant_id"] = str(value["tenant_id"])
        value["request_json"] = _json_text(value.pop("request", None))
        value["result_json"] = _json_text(value.pop("result", None), "null")
        value["cancel_requested"] = bool(value.get("cancel_requested"))
        if value.get("parent_job_id") is not None:
            value["parent_job_id"] = str(value["parent_job_id"])
        return value

    @classmethod
    def _decision_row(cls, row: dict | None) -> dict | None:
        if row is None:
            return None
        value = {key: _iso(item) for key, item in dict(row).items()}
        for key in ("id", "audit_id", "supersedes_decision_id"):
            if value.get(key) is not None:
                value[key] = str(value[key])
        return value

    @classmethod
    def _correction_row(cls, row: dict | None) -> dict | None:
        if row is None:
            return None
        value = {key: _iso(item) for key, item in dict(row).items()}
        for key in ("id", "audit_id", "supersedes_correction_id"):
            if value.get(key) is not None:
                value[key] = str(value[key])
        value["reference_ids_json"] = _json_text(value.pop("reference_ids", None), "[]")
        value["proposed_value_json"] = _json_text(value.pop("proposed_value", None))
        return value

    @classmethod
    def _activity_row(cls, row: dict | None) -> dict | None:
        if row is None:
            return None
        value = {key: _iso(item) for key, item in dict(row).items()}
        if value.get("audit_id") is not None:
            value["audit_id"] = str(value["audit_id"])
        value["payload_json"] = _json_text(value.pop("payload", None))
        return value

    # ------------------------------------------------------------------
    # append-only activity chain (identical hashing to the SQLite store)
    # ------------------------------------------------------------------
    @staticmethod
    def _activity(
        cursor,
        tenant_id: str,
        audit_id: str | None,
        actor_id: str,
        action: str,
        entity_type: str,
        entity_id: str,
        payload: dict,
    ) -> None:
        created_at = utc_now()
        cursor.execute(
            "SELECT event_hash FROM activity WHERE tenant_id=%s ORDER BY id DESC LIMIT 1",
            (tenant_id,),
        )
        previous = cursor.fetchone()
        previous_hash = previous["event_hash"] if previous else ZERO_HASH
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
        cursor.execute(
            """INSERT INTO activity
               (tenant_id,audit_id,actor_id,action,entity_type,entity_id,payload,
                previous_hash,event_hash,created_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)""",
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
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT * FROM activity WHERE tenant_id=%s ORDER BY id", (tenant_id,)
            )
            rows = cursor.fetchall()
        previous_hash = ZERO_HASH
        for position, value in enumerate(rows, 1):
            row = dict(value)
            event = {
                "tenant_id": str(row["tenant_id"]),
                "audit_id": str(row["audit_id"]) if row["audit_id"] else None,
                "actor_id": row["actor_id"],
                "action": row["action"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "payload": row["payload"] if isinstance(row["payload"], dict)
                else json.loads(row["payload"]),
                "previous_hash": row["previous_hash"],
                "created_at": _iso(row["created_at"]),
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

    def list_activity(
        self, tenant_id: str, audit_id: str | None = None, limit: int = 100
    ) -> list[dict]:
        limit = max(1, min(500, int(limit)))
        with self._cursor() as cursor:
            if audit_id:
                cursor.execute(
                    """SELECT * FROM activity WHERE tenant_id=%s AND audit_id=%s
                       ORDER BY id DESC LIMIT %s""",
                    (tenant_id, audit_id, limit),
                )
            else:
                cursor.execute(
                    "SELECT * FROM activity WHERE tenant_id=%s ORDER BY id DESC LIMIT %s",
                    (tenant_id, limit),
                )
            return [self._activity_row(row) for row in cursor.fetchall()]  # type: ignore[misc]

    # ------------------------------------------------------------------
    # organizations and membership
    # ------------------------------------------------------------------
    def ensure_organization(
        self, tenant_id: str, name: str, *, actor_id: str = "system"
    ) -> dict:
        with self._cursor() as cursor:
            cursor.execute(
                """INSERT INTO organizations(tenant_id,name) VALUES(%s,%s)
                   ON CONFLICT (tenant_id) DO UPDATE SET updated_at=CURRENT_TIMESTAMP
                   RETURNING *, (xmax = 0) AS inserted""",
                (tenant_id, name[:200] or "Organization"),
            )
            row = dict(cursor.fetchone())
            if row.pop("inserted", False):
                self._activity(
                    cursor, tenant_id, None, actor_id, "organization.created",
                    "organization", tenant_id, {"name": row["name"]},
                )
            return self._row(row)  # type: ignore[return-value]

    def get_organization(self, tenant_id: str) -> dict | None:
        with self._cursor() as cursor:
            cursor.execute("SELECT * FROM organizations WHERE tenant_id=%s", (tenant_id,))
            return self._row(cursor.fetchone())

    def upsert_member(
        self,
        tenant_id: str,
        subject: str,
        *,
        display_name: str | None,
        email: str | None,
        roles: list[str],
        actor_id: str,
    ) -> dict:
        cleaned = sorted({str(role).strip().lower() for role in roles if str(role).strip()})
        with self._cursor() as cursor:
            cursor.execute(
                "INSERT INTO organizations(tenant_id,name) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                (tenant_id, "Organization"),
            )
            cursor.execute(
                """INSERT INTO organization_members
                   (tenant_id,subject,display_name,email,roles,active)
                   VALUES(%s,%s,%s,%s,%s,TRUE)
                   ON CONFLICT (tenant_id,subject) DO UPDATE
                   SET display_name=COALESCE(EXCLUDED.display_name,
                                             organization_members.display_name),
                       email=COALESCE(EXCLUDED.email, organization_members.email),
                       roles=EXCLUDED.roles, active=TRUE, updated_at=CURRENT_TIMESTAMP
                   RETURNING *""",
                (tenant_id, subject[:255], display_name, email, cleaned),
            )
            row = dict(cursor.fetchone())
            self._activity(
                cursor, tenant_id, None, actor_id, "member.upserted", "member",
                subject[:255], {"roles": cleaned},
            )
            return self._row(row)  # type: ignore[return-value]

    def list_members(self, tenant_id: str) -> list[dict]:
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT * FROM organization_members WHERE tenant_id=%s ORDER BY subject",
                (tenant_id,),
            )
            return self._rows(cursor.fetchall())

    def deactivate_member(self, tenant_id: str, subject: str, actor_id: str) -> dict:
        with self._cursor() as cursor:
            cursor.execute(
                """UPDATE organization_members SET active=FALSE, updated_at=CURRENT_TIMESTAMP
                   WHERE tenant_id=%s AND subject=%s RETURNING *""",
                (tenant_id, subject),
            )
            row = self._one(cursor.fetchone(), "member")
            self._activity(
                cursor, tenant_id, None, actor_id, "member.deactivated", "member", subject, {}
            )
            return self._row(row)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # manuscripts
    # ------------------------------------------------------------------
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
        object_backend: str = "local",
        object_key: str | None = None,
        object_version_id: str | None = None,
        text_layer_status: str = "unknown",
    ) -> tuple[dict, bool]:
        with self._cursor() as cursor:
            cursor.execute(
                "INSERT INTO organizations(tenant_id,name) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                (tenant_id, "Organization"),
            )
            cursor.execute(
                "SELECT * FROM manuscripts WHERE tenant_id=%s AND content_sha256=%s",
                (tenant_id, content_sha256),
            )
            existing = cursor.fetchone()
            if existing:
                return self._manuscript_row(existing), False  # type: ignore[return-value]
            identifier = self._id()
            cursor.execute(
                """INSERT INTO manuscripts
                   (tenant_id,id,legacy_document_id,title,original_filename,content_sha256,
                    pdf_path,object_backend,object_key,object_version_id,text_layer_status)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (
                    tenant_id,
                    identifier,
                    legacy_document_id,
                    title,
                    original_filename,
                    content_sha256,
                    pdf_path,
                    object_backend,
                    object_key,
                    object_version_id,
                    text_layer_status,
                ),
            )
            row = dict(cursor.fetchone())
            self._activity(
                cursor, tenant_id, None, actor_id, "manuscript.created", "manuscript",
                identifier, {"content_sha256": content_sha256, "object_backend": object_backend},
            )
            return self._manuscript_row(row), True  # type: ignore[return-value]

    def list_manuscripts(self, tenant_id: str) -> list[dict]:
        with self._cursor() as cursor:
            cursor.execute(
                """SELECT m.*, a.id AS latest_audit_id, a.state AS latest_audit_state,
                          COALESCE((SELECT COUNT(*) FROM decisions d
                                    WHERE d.tenant_id=m.tenant_id AND d.audit_id=a.id), 0)
                              AS decision_count
                   FROM manuscripts m
                   LEFT JOIN LATERAL (
                       SELECT a2.id, a2.state FROM audits a2
                       WHERE a2.tenant_id=m.tenant_id AND a2.manuscript_id=m.id
                       ORDER BY a2.created_at DESC LIMIT 1
                   ) a ON TRUE
                   WHERE m.tenant_id=%s ORDER BY m.updated_at DESC""",
                (tenant_id,),
            )
            rows = []
            for row in cursor.fetchall():
                value = self._manuscript_row(row)
                if value and value.get("latest_audit_id"):
                    value["latest_audit_id"] = str(value["latest_audit_id"])
                rows.append(value)
            return rows

    def get_manuscript(self, tenant_id: str, manuscript_id: str) -> dict:
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT * FROM manuscripts WHERE tenant_id=%s AND id=%s",
                (tenant_id, manuscript_id),
            )
            return self._manuscript_row(self._one(cursor.fetchone(), "manuscript"))  # type: ignore

    def update_manuscript_title(self, tenant_id: str, manuscript_id: str, title: str) -> None:
        title = title.strip()[:1000]
        if not title:
            return
        with self._cursor() as cursor:
            cursor.execute(
                """UPDATE manuscripts SET title=%s, updated_at=CURRENT_TIMESTAMP
                   WHERE tenant_id=%s AND id=%s""",
                (title, tenant_id, manuscript_id),
            )

    def update_manuscript_text_layer(
        self, tenant_id: str, manuscript_id: str, status: str
    ) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                """UPDATE manuscripts SET text_layer_status=%s, updated_at=CURRENT_TIMESTAMP
                   WHERE tenant_id=%s AND id=%s""",
                (status, tenant_id, manuscript_id),
            )

    # ------------------------------------------------------------------
    # audits and jobs
    # ------------------------------------------------------------------
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
        audit_id, job_id = self._id(), self._id()
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT id FROM manuscripts WHERE tenant_id=%s AND id=%s",
                (tenant_id, manuscript_id),
            )
            self._one(cursor.fetchone(), "manuscript")
            cursor.execute(
                """INSERT INTO audits(tenant_id,id,manuscript_id,job_id,state,summary)
                   VALUES(%s,%s,%s,%s,%s,'{}'::jsonb) RETURNING *""",
                (tenant_id, audit_id, manuscript_id, job_id, AuditState.QUEUED.value),
            )
            audit = dict(cursor.fetchone())
            cursor.execute(
                """INSERT INTO jobs
                   (tenant_id,id,audit_id,kind,state,priority,progress,stage,request,
                    max_attempts,parent_job_id)
                   VALUES(%s,%s,%s,%s,%s,%s,0,'queued',%s::jsonb,%s,%s) RETURNING *""",
                (
                    tenant_id,
                    job_id,
                    audit_id,
                    kind,
                    JobState.QUEUED.value,
                    max(0, min(1000, int(priority))),
                    encode(request),
                    max(1, min(10, int(max_attempts))),
                    request.get("retry_of_job_id"),
                ),
            )
            job = dict(cursor.fetchone())
            self._activity(
                cursor, tenant_id, audit_id, actor_id, "audit.queued", "audit", audit_id,
                {"job_id": job_id, "kind": kind},
            )
        return self._audit_row(audit), self._job_row(job)  # type: ignore[return-value]

    def get_audit(self, tenant_id: str, audit_id: str) -> dict:
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT * FROM audits WHERE tenant_id=%s AND id=%s", (tenant_id, audit_id)
            )
            return self._audit_row(self._one(cursor.fetchone(), "audit"))  # type: ignore

    def list_audits(self, tenant_id: str, manuscript_id: str) -> list[dict]:
        self.get_manuscript(tenant_id, manuscript_id)
        with self._cursor() as cursor:
            cursor.execute(
                """SELECT * FROM audits WHERE tenant_id=%s AND manuscript_id=%s
                   ORDER BY created_at DESC""",
                (tenant_id, manuscript_id),
            )
            return [self._audit_row(row) for row in cursor.fetchall()]  # type: ignore[misc]

    def get_job(self, tenant_id: str, job_id: str) -> dict:
        with self._cursor() as cursor:
            cursor.execute("SELECT * FROM jobs WHERE tenant_id=%s AND id=%s", (tenant_id, job_id))
            return self._job_row(self._one(cursor.fetchone(), "job"))  # type: ignore

    def list_queued_jobs(self) -> list[dict]:
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT * FROM jobs WHERE state=%s ORDER BY priority, available_at, created_at",
                (JobState.QUEUED.value,),
            )
            return [self._job_row(row) for row in cursor.fetchall()]  # type: ignore[misc]

    # -- durable queue --------------------------------------------------
    def lease_next_job(
        self, worker_id: str, *, lease_seconds: int = 120, kinds: tuple[str, ...] = ("audit",)
    ) -> dict | None:
        """Atomically take the highest-priority runnable job for this worker."""
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH claimed AS (
                        SELECT tenant_id, id FROM jobs
                        WHERE state = %s
                          AND available_at <= CURRENT_TIMESTAMP
                          AND kind = ANY(%s)
                        ORDER BY priority, available_at, created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE jobs j
                       SET state = %s,
                           stage = 'starting',
                           progress = GREATEST(j.progress, 1),
                           attempts = j.attempts + 1,
                           lease_owner = %s,
                           lease_expires_at = CURRENT_TIMESTAMP + make_interval(secs => %s),
                           heartbeat_at = CURRENT_TIMESTAMP,
                           started_at = COALESCE(j.started_at, CURRENT_TIMESTAMP)
                      FROM claimed c
                     WHERE j.tenant_id = c.tenant_id AND j.id = c.id
                    RETURNING j.*
                    """,
                    (
                        JobState.QUEUED.value,
                        list(kinds),
                        JobState.RUNNING.value,
                        worker_id[:200],
                        float(max(5, lease_seconds)),
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    connection.commit()
                    return None
                job = dict(row)
                cursor.execute(
                    """UPDATE audits SET state=%s, started_at=COALESCE(started_at,CURRENT_TIMESTAMP)
                       WHERE tenant_id=%s AND id=%s""",
                    (AuditState.PROCESSING.value, job["tenant_id"], job["audit_id"]),
                )
                self._activity(
                    cursor, str(job["tenant_id"]), str(job["audit_id"]), worker_id[:200],
                    "audit.started", "job", str(job["id"]), {"attempt": job["attempts"]},
                )
            connection.commit()
        return self._job_row(job)

    def claim_job(self, tenant_id: str, job_id: str, worker_id: str) -> dict | None:
        """Claim one named job. Returns None when another worker already holds it."""
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """UPDATE jobs SET state=%s, stage='starting',
                              progress=GREATEST(progress,1), attempts=attempts+1,
                              lease_owner=%s,
                              lease_expires_at=CURRENT_TIMESTAMP + make_interval(secs => 120),
                              heartbeat_at=CURRENT_TIMESTAMP,
                              started_at=COALESCE(started_at,CURRENT_TIMESTAMP)
                       WHERE tenant_id=%s AND id=%s AND state=%s RETURNING *""",
                    (
                        JobState.RUNNING.value,
                        worker_id[:200],
                        tenant_id,
                        job_id,
                        JobState.QUEUED.value,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    connection.rollback()
                    return None
                job = dict(row)
                cursor.execute(
                    """UPDATE audits SET state=%s, started_at=COALESCE(started_at,CURRENT_TIMESTAMP)
                       WHERE tenant_id=%s AND id=%s""",
                    (AuditState.PROCESSING.value, tenant_id, job["audit_id"]),
                )
                self._activity(
                    cursor, tenant_id, str(job["audit_id"]), worker_id[:200], "audit.started",
                    "job", job_id, {"attempt": job["attempts"]},
                )
            connection.commit()
        return self._job_row(job)

    def heartbeat_job(
        self, tenant_id: str, job_id: str, worker_id: str, *, lease_seconds: int = 120
    ) -> bool:
        with self._cursor() as cursor:
            cursor.execute(
                """UPDATE jobs
                      SET heartbeat_at=CURRENT_TIMESTAMP,
                          lease_expires_at=CURRENT_TIMESTAMP + make_interval(secs => %s)
                    WHERE tenant_id=%s AND id=%s AND lease_owner=%s AND state IN (%s,%s)""",
                (
                    float(max(5, lease_seconds)),
                    tenant_id,
                    job_id,
                    worker_id[:200],
                    JobState.RUNNING.value,
                    JobState.CANCEL_REQUESTED.value,
                ),
            )
            return cursor.rowcount > 0

    def reclaim_expired_leases(self) -> int:
        """Return crashed work to the queue, or dead-letter it when exhausted."""
        with self._cursor() as cursor:
            cursor.execute(
                """UPDATE jobs
                      SET state = CASE WHEN attempts >= max_attempts THEN 'dead_letter'
                                       ELSE %s END,
                          stage = CASE WHEN attempts >= max_attempts THEN 'dead_letter'
                                       ELSE 'recovered_after_lease_expiry' END,
                          lease_owner = NULL,
                          lease_expires_at = NULL,
                          available_at = CURRENT_TIMESTAMP,
                          error_code = CASE WHEN attempts >= max_attempts
                                            THEN 'lease_expired' ELSE error_code END,
                          error_message = CASE WHEN attempts >= max_attempts
                                               THEN 'The worker stopped responding and the retry budget is exhausted.'
                                               ELSE error_message END,
                          completed_at = CASE WHEN attempts >= max_attempts
                                              THEN CURRENT_TIMESTAMP ELSE completed_at END
                    WHERE state IN (%s,%s) AND lease_expires_at IS NOT NULL
                      AND lease_expires_at < CURRENT_TIMESTAMP
                RETURNING tenant_id, id, audit_id, state""",
                (JobState.QUEUED.value, JobState.RUNNING.value, JobState.CANCEL_REQUESTED.value),
            )
            rows = [dict(row) for row in cursor.fetchall()]
            for row in rows:
                audit_state = (
                    AuditState.FAILED.value
                    if row["state"] == "dead_letter"
                    else AuditState.QUEUED.value
                )
                cursor.execute(
                    "UPDATE audits SET state=%s WHERE tenant_id=%s AND id=%s",
                    (audit_state, row["tenant_id"], row["audit_id"]),
                )
                self._activity(
                    cursor, str(row["tenant_id"]), str(row["audit_id"]), "queue-supervisor",
                    "job.lease_expired", "job", str(row["id"]), {"next_state": row["state"]},
                )
            return len(rows)

    def recover_interrupted_jobs(self) -> int:
        """Compatibility shim used at API start-up; the queue reclaims by lease."""
        return self.reclaim_expired_leases()

    def update_job_progress(self, tenant_id: str, job_id: str, progress: int, stage: str) -> bool:
        progress = max(1, min(99, int(progress)))
        with self._cursor() as cursor:
            cursor.execute(
                """UPDATE jobs SET progress=%s, stage=%s, heartbeat_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=%s AND id=%s AND state=%s""",
                (progress, stage[:200], tenant_id, job_id, JobState.RUNNING.value),
            )
            return cursor.rowcount > 0

    def job_cancel_requested(self, tenant_id: str, job_id: str) -> bool:
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT cancel_requested FROM jobs WHERE tenant_id=%s AND id=%s",
                (tenant_id, job_id),
            )
            return bool(self._one(cursor.fetchone(), "job")["cancel_requested"])

    def request_cancel(self, tenant_id: str, job_id: str, actor_id: str) -> dict:
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT * FROM jobs WHERE tenant_id=%s AND id=%s FOR UPDATE",
                (tenant_id, job_id),
            )
            row = self._one(cursor.fetchone(), "job")
            if row["state"] in TERMINAL_JOB_STATES:
                raise WorkspaceConflict("completed jobs cannot be cancelled")
            next_state = (
                JobState.CANCELLED.value
                if row["state"] == JobState.QUEUED.value
                else JobState.CANCEL_REQUESTED.value
            )
            cursor.execute(
                """UPDATE jobs SET state=%s, cancel_requested=TRUE,
                          completed_at=CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE completed_at END
                    WHERE tenant_id=%s AND id=%s RETURNING *""",
                (next_state, next_state == JobState.CANCELLED.value, tenant_id, job_id),
            )
            result = dict(cursor.fetchone())
            if next_state == JobState.CANCELLED.value:
                cursor.execute(
                    """UPDATE audits SET state=%s, completed_at=CURRENT_TIMESTAMP
                        WHERE tenant_id=%s AND id=%s""",
                    (AuditState.CANCELLED.value, tenant_id, row["audit_id"]),
                )
            self._activity(
                cursor, tenant_id, str(row["audit_id"]), actor_id, "audit.cancel_requested",
                "job", job_id, {},
            )
            return self._job_row(result)  # type: ignore[return-value]

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
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT * FROM jobs WHERE tenant_id=%s AND id=%s FOR UPDATE", (tenant_id, job_id)
            )
            row = self._one(cursor.fetchone(), "job")
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
            cursor.execute(
                """UPDATE jobs SET state=%s, progress=%s, stage=%s, error_code=%s,
                          error_message=%s, result=%s::jsonb, lease_owner=NULL,
                          lease_expires_at=NULL, completed_at=CURRENT_TIMESTAMP,
                          heartbeat_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=%s AND id=%s""",
                (
                    job_state,
                    100 if success and not cancelled else row["progress"],
                    "completed" if success else "cancelled" if cancelled else "failed",
                    error_code,
                    error_message,
                    encode(result or {}),
                    tenant_id,
                    job_id,
                ),
            )
            cursor.execute(
                """UPDATE audits SET state=%s, artifact_path=%s, artifact_key=%s, summary=%s::jsonb,
                          error_code=%s, error_message=%s, completed_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=%s AND id=%s""",
                (
                    audit_state,
                    artifact_path,
                    artifact_key,
                    encode(summary or {}),
                    error_code,
                    error_message,
                    tenant_id,
                    row["audit_id"],
                ),
            )
            action = (
                "audit.cancelled" if cancelled else "audit.completed" if success else "audit.failed"
            )
            self._activity(
                cursor, tenant_id, str(row["audit_id"]), row["lease_owner"] or "worker", action,
                "audit", str(row["audit_id"]), {"error_code": error_code},
            )

    def release_job(
        self,
        tenant_id: str,
        job_id: str,
        worker_id: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        backoff_seconds: float = 0.0,
        max_attempts: int = 3,
    ) -> str:
        """Return a transient failure to the queue, or fail it permanently."""
        del max_attempts  # the per-row budget is authoritative
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT * FROM jobs WHERE tenant_id=%s AND id=%s FOR UPDATE", (tenant_id, job_id)
            )
            row = self._one(cursor.fetchone(), "job")
            exhausted = int(row["attempts"]) >= int(row["max_attempts"])
            if exhausted or row["cancel_requested"]:
                self.finish_job(
                    tenant_id,
                    job_id,
                    success=False,
                    error_code=error_code,
                    error_message=error_message,
                )
                return "failed"
            cursor.execute(
                """UPDATE jobs SET state=%s, stage='retry_scheduled', lease_owner=NULL,
                          lease_expires_at=NULL,
                          available_at=CURRENT_TIMESTAMP + make_interval(secs => %s),
                          error_code=%s, error_message=%s
                    WHERE tenant_id=%s AND id=%s""",
                (
                    JobState.QUEUED.value,
                    float(max(0.0, backoff_seconds)),
                    error_code,
                    error_message,
                    tenant_id,
                    job_id,
                ),
            )
            cursor.execute(
                "UPDATE audits SET state=%s WHERE tenant_id=%s AND id=%s",
                (AuditState.QUEUED.value, tenant_id, row["audit_id"]),
            )
            self._activity(
                cursor, tenant_id, str(row["audit_id"]), worker_id[:200], "job.retry_scheduled",
                "job", job_id, {"attempt": row["attempts"], "error_code": error_code},
            )
            return "requeued"

    def retry_job(self, tenant_id: str, job_id: str, actor_id: str) -> tuple[dict, dict]:
        previous = self.get_job(tenant_id, job_id)
        if previous["state"] not in {
            JobState.FAILED.value,
            JobState.CANCELLED.value,
            "dead_letter",
        }:
            raise WorkspaceConflict("only failed, cancelled or dead-lettered jobs can be retried")
        audit = self.get_audit(tenant_id, previous["audit_id"])
        request = json.loads(previous["request_json"])
        request["retry_of_job_id"] = job_id
        return self.create_audit_job(tenant_id, audit["manuscript_id"], request, actor_id)

    def queue_depth(self, tenant_id: str | None = None) -> dict:
        with self._cursor() as cursor:
            if tenant_id:
                cursor.execute(
                    "SELECT state, COUNT(*) AS total FROM jobs WHERE tenant_id=%s GROUP BY state",
                    (tenant_id,),
                )
            else:
                cursor.execute("SELECT state, COUNT(*) AS total FROM jobs GROUP BY state")
            counts = {row["state"]: int(row["total"]) for row in cursor.fetchall()}
            cursor.execute(
                """SELECT COALESCE(EXTRACT(EPOCH FROM
                          (CURRENT_TIMESTAMP - MIN(created_at))), 0) AS oldest
                     FROM jobs WHERE state=%s""",
                (JobState.QUEUED.value,),
            )
            oldest = float((cursor.fetchone() or {}).get("oldest") or 0.0)
        return {
            "counts": counts,
            "queued": counts.get(JobState.QUEUED.value, 0),
            "running": counts.get(JobState.RUNNING.value, 0),
            "dead_letter": counts.get("dead_letter", 0),
            "oldest_queued_seconds": round(oldest, 2),
        }

    # ------------------------------------------------------------------
    # human decisions and corrections
    # ------------------------------------------------------------------
    def latest_decisions(self, tenant_id: str, audit_id: str) -> dict[str, dict]:
        self.get_audit(tenant_id, audit_id)
        with self._cursor() as cursor:
            cursor.execute(
                """SELECT d.* FROM decisions d
                    WHERE d.tenant_id=%s AND d.audit_id=%s
                      AND NOT EXISTS (SELECT 1 FROM decisions child
                          WHERE child.tenant_id=d.tenant_id AND child.supersedes_decision_id=d.id)
                    ORDER BY d.created_at""",
                (tenant_id, audit_id),
            )
            return {
                row["finding_id"]: self._decision_row(row)  # type: ignore[misc]
                for row in cursor.fetchall()
            }

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
        identifier = self._id()
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT id FROM audits WHERE tenant_id=%s AND id=%s FOR UPDATE",
                (tenant_id, audit_id),
            )
            self._one(cursor.fetchone(), "audit")
            cursor.execute(
                "SELECT * FROM decisions WHERE tenant_id=%s AND idempotency_key=%s",
                (tenant_id, idempotency_key),
            )
            replay = cursor.fetchone()
            if replay:
                return self._decision_row(replay)  # type: ignore[return-value]
            cursor.execute(
                """SELECT d.* FROM decisions d
                    WHERE d.tenant_id=%s AND d.audit_id=%s AND d.finding_id=%s
                      AND NOT EXISTS (SELECT 1 FROM decisions child
                          WHERE child.tenant_id=d.tenant_id AND child.supersedes_decision_id=d.id)
                    ORDER BY d.created_at DESC LIMIT 1""",
                (tenant_id, audit_id, finding_id),
            )
            current = cursor.fetchone()
            current_id = str(current["id"]) if current else None
            if current_id != expected_current_id:
                raise WorkspaceConflict("the finding decision changed; refresh before saving")
            cursor.execute(
                """INSERT INTO decisions
                   (tenant_id,id,audit_id,finding_id,disposition,rationale,reviewer_id,
                    supersedes_decision_id,idempotency_key)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
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
                ),
            )
            row = dict(cursor.fetchone())
            self._activity(
                cursor, tenant_id, audit_id, reviewer_id, "decision.recorded", "finding",
                finding_id,
                {
                    "decision_id": identifier,
                    "disposition": disposition,
                    "supersedes_decision_id": current_id,
                },
            )
            return self._decision_row(row)  # type: ignore[return-value]

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
        identifier = self._id()
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT id FROM audits WHERE tenant_id=%s AND id=%s FOR UPDATE",
                (tenant_id, audit_id),
            )
            self._one(cursor.fetchone(), "audit")
            cursor.execute(
                "SELECT * FROM corrections WHERE tenant_id=%s AND idempotency_key=%s",
                (tenant_id, idempotency_key),
            )
            replay = cursor.fetchone()
            if replay:
                return self._correction_row(replay)  # type: ignore[return-value]
            if supersedes_id:
                cursor.execute(
                    "SELECT * FROM corrections WHERE tenant_id=%s AND id=%s FOR UPDATE",
                    (tenant_id, supersedes_id),
                )
                parent = self._one(cursor.fetchone(), "correction")
                if str(parent["audit_id"]) != str(audit_id):
                    raise WorkspaceConflict("correction supersession must stay in one audit")
                cursor.execute(
                    "UPDATE corrections SET state=%s WHERE tenant_id=%s AND id=%s",
                    (CorrectionState.SUPERSEDED.value, tenant_id, supersedes_id),
                )
            cursor.execute(
                """INSERT INTO corrections
                   (tenant_id,id,audit_id,kind,reference_ids,proposed_value,rationale,
                    reviewer_id,state,supersedes_correction_id,idempotency_key)
                   VALUES(%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s) RETURNING *""",
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
                ),
            )
            row = dict(cursor.fetchone())
            self._activity(
                cursor, tenant_id, audit_id, reviewer_id, "correction.proposed", "correction",
                identifier, {"kind": kind, "reference_ids": reference_ids},
            )
            return self._correction_row(row)  # type: ignore[return-value]

    def transition_correction(
        self, tenant_id: str, correction_id: str, state: str, rationale: str, actor_id: str
    ) -> dict:
        if state not in {CorrectionState.ACCEPTED.value, CorrectionState.REJECTED.value}:
            raise ValueError("invalid correction transition")
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT * FROM corrections WHERE tenant_id=%s AND id=%s FOR UPDATE",
                (tenant_id, correction_id),
            )
            row = self._one(cursor.fetchone(), "correction")
            if row["state"] != CorrectionState.PROPOSED.value:
                raise WorkspaceConflict("only proposed corrections can be decided")
            cursor.execute(
                "UPDATE corrections SET state=%s WHERE tenant_id=%s AND id=%s RETURNING *",
                (state, tenant_id, correction_id),
            )
            result = dict(cursor.fetchone())
            self._activity(
                cursor, tenant_id, str(row["audit_id"]), actor_id, f"correction.{state}",
                "correction", correction_id, {"rationale": rationale},
            )
            return self._correction_row(result)  # type: ignore[return-value]

    def list_corrections(self, tenant_id: str, audit_id: str) -> list[dict]:
        self.get_audit(tenant_id, audit_id)
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT * FROM corrections WHERE tenant_id=%s AND audit_id=%s ORDER BY created_at",
                (tenant_id, audit_id),
            )
            return [self._correction_row(row) for row in cursor.fetchall()]  # type: ignore[misc]

    def list_review_cases(self, tenant_id: str, *, limit: int = 200, **_ignored) -> list[dict]:
        with self._cursor() as cursor:
            cursor.execute(
                """SELECT a.*, m.title AS manuscript_title FROM audits a
                    JOIN manuscripts m ON m.tenant_id=a.tenant_id AND m.id=a.manuscript_id
                    WHERE a.tenant_id=%s AND a.artifact_path IS NOT NULL
                    ORDER BY a.completed_at DESC NULLS LAST LIMIT %s""",
                (tenant_id, min(max(int(limit), 1), 500)),
            )
            return [self._audit_row(row) for row in cursor.fetchall()]  # type: ignore[misc]

    # ------------------------------------------------------------------
    # assignment
    # ------------------------------------------------------------------
    def assign_case(
        self,
        tenant_id: str,
        audit_id: str,
        finding_id: str,
        assignee: str,
        *,
        actor_id: str,
        note: str | None = None,
        due_at: str | None = None,
    ) -> dict:
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT id FROM audits WHERE tenant_id=%s AND id=%s", (tenant_id, audit_id)
            )
            self._one(cursor.fetchone(), "audit")
            cursor.execute(
                """SELECT active FROM organization_members WHERE tenant_id=%s AND subject=%s""",
                (tenant_id, assignee),
            )
            member = cursor.fetchone()
            if member is not None and not member["active"]:
                raise WorkspaceConflict("the assignee is not an active member of this organization")
            cursor.execute(
                """INSERT INTO assignments
                   (tenant_id,audit_id,finding_id,assignee,assigned_by,note,due_at,state)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,'assigned')
                   ON CONFLICT (tenant_id,audit_id,finding_id) DO UPDATE
                   SET assignee=EXCLUDED.assignee, assigned_by=EXCLUDED.assigned_by,
                       note=EXCLUDED.note, due_at=EXCLUDED.due_at, state='assigned',
                       updated_at=CURRENT_TIMESTAMP
                   RETURNING *""",
                (tenant_id, audit_id, finding_id, assignee[:255], actor_id, note, due_at),
            )
            row = dict(cursor.fetchone())
            self._activity(
                cursor, tenant_id, audit_id, actor_id, "case.assigned", "finding", finding_id,
                {"assignee": assignee[:255]},
            )
            return self._row(row)  # type: ignore[return-value]

    def unassign_case(
        self, tenant_id: str, audit_id: str, finding_id: str, actor_id: str
    ) -> dict:
        with self._cursor() as cursor:
            cursor.execute(
                """UPDATE assignments SET state='released', updated_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=%s AND audit_id=%s AND finding_id=%s RETURNING *""",
                (tenant_id, audit_id, finding_id),
            )
            row = self._one(cursor.fetchone(), "assignment")
            self._activity(
                cursor, tenant_id, audit_id, actor_id, "case.released", "finding", finding_id, {}
            )
            return self._row(row)  # type: ignore[return-value]

    def list_assignments(
        self, tenant_id: str, *, audit_id: str | None = None, assignee: str | None = None
    ) -> list[dict]:
        clauses, params = ["tenant_id=%s"], [tenant_id]
        if audit_id:
            clauses.append("audit_id=%s")
            params.append(audit_id)
        if assignee:
            clauses.append("assignee=%s")
            params.append(assignee)
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM assignments WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC",
                tuple(params),
            )
            rows = []
            for row in cursor.fetchall():
                value = self._row(row)
                if value:
                    value["audit_id"] = str(value["audit_id"])
                    rows.append(value)
            return rows

    def assignment_workload(self, tenant_id: str) -> list[dict]:
        with self._cursor() as cursor:
            cursor.execute(
                """SELECT m.subject, m.display_name, m.roles, m.active,
                          COALESCE(counts.open_cases, 0) AS open_cases
                     FROM organization_members m
                     LEFT JOIN (
                        SELECT assignee, COUNT(*) AS open_cases FROM assignments
                        WHERE tenant_id=%s AND state='assigned' GROUP BY assignee
                     ) counts ON counts.assignee = m.subject
                    WHERE m.tenant_id=%s
                    ORDER BY open_cases ASC, m.subject ASC""",
                (tenant_id, tenant_id),
            )
            return self._rows(cursor.fetchall())

    # ------------------------------------------------------------------
    # repairs
    # ------------------------------------------------------------------
    def record_repair(
        self,
        tenant_id: str,
        *,
        audit_id: str,
        finding_id: str | None,
        issue_category: str,
        action: str,
        state: str,
        detail: dict,
        requested_by: str,
        idempotency_key: str | None = None,
        follow_up_job_id: str | None = None,
        follow_up_audit_id: str | None = None,
        attempt: int = 1,
    ) -> dict:
        identifier = self._id()
        with self._cursor() as cursor:
            if idempotency_key:
                cursor.execute(
                    "SELECT * FROM repairs WHERE tenant_id=%s AND idempotency_key=%s",
                    (tenant_id, idempotency_key),
                )
                replay = cursor.fetchone()
                if replay:
                    return self._repair_row(replay)  # type: ignore[return-value]
            cursor.execute(
                """INSERT INTO repairs
                   (tenant_id,id,audit_id,finding_id,issue_category,action,state,attempt,
                    follow_up_job_id,follow_up_audit_id,detail,requested_by,idempotency_key)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s) RETURNING *""",
                (
                    tenant_id,
                    identifier,
                    audit_id,
                    finding_id,
                    issue_category,
                    action,
                    state,
                    attempt,
                    follow_up_job_id,
                    follow_up_audit_id,
                    encode(detail),
                    requested_by,
                    idempotency_key,
                ),
            )
            row = dict(cursor.fetchone())
            self._activity(
                cursor, tenant_id, audit_id, requested_by, f"repair.{state}", "repair",
                identifier, {"issue_category": issue_category, "action": action},
            )
            return self._repair_row(row)  # type: ignore[return-value]

    def update_repair(self, tenant_id: str, repair_id: str, **fields: Any) -> dict:
        allowed = {"state", "detail", "follow_up_job_id", "follow_up_audit_id", "attempt"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_repair(tenant_id, repair_id)
        assignments, params = [], []
        for key, value in updates.items():
            if key == "detail":
                assignments.append("detail=%s::jsonb")
                params.append(encode(value))
            else:
                assignments.append(f"{key}=%s")
                params.append(value)
        if updates.get("state") in {"succeeded", "failed", "not_applicable"}:
            assignments.append("completed_at=CURRENT_TIMESTAMP")
        params.extend([tenant_id, repair_id])
        with self._cursor() as cursor:
            cursor.execute(
                f"UPDATE repairs SET {', '.join(assignments)} "
                "WHERE tenant_id=%s AND id=%s RETURNING *",
                tuple(params),
            )
            return self._repair_row(self._one(cursor.fetchone(), "repair"))  # type: ignore

    def get_repair(self, tenant_id: str, repair_id: str) -> dict:
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT * FROM repairs WHERE tenant_id=%s AND id=%s", (tenant_id, repair_id)
            )
            return self._repair_row(self._one(cursor.fetchone(), "repair"))  # type: ignore

    def list_repairs(self, tenant_id: str, audit_id: str | None = None) -> list[dict]:
        with self._cursor() as cursor:
            if audit_id:
                cursor.execute(
                    """SELECT * FROM repairs WHERE tenant_id=%s AND audit_id=%s
                        ORDER BY created_at DESC""",
                    (tenant_id, audit_id),
                )
            else:
                cursor.execute(
                    "SELECT * FROM repairs WHERE tenant_id=%s ORDER BY created_at DESC LIMIT 200",
                    (tenant_id,),
                )
            return [self._repair_row(row) for row in cursor.fetchall()]  # type: ignore[misc]

    @classmethod
    def _repair_row(cls, row: dict | None) -> dict | None:
        if row is None:
            return None
        value = {key: _iso(item) for key, item in dict(row).items()}
        for key in ("id", "audit_id", "follow_up_job_id", "follow_up_audit_id"):
            if value.get(key) is not None:
                value[key] = str(value[key])
        value["detail"] = (
            value["detail"] if isinstance(value.get("detail"), dict)
            else json.loads(value.get("detail") or "{}")
        )
        return value

    # ------------------------------------------------------------------
    # exports
    # ------------------------------------------------------------------
    def record_export(self, tenant_id: str, **fields: Any) -> dict:
        identifier = self._id()
        expires_at = None
        retention_days = int(fields.get("retention_days") or 0)
        if retention_days > 0:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(days=retention_days)
            ).isoformat()
        with self._cursor() as cursor:
            cursor.execute(
                """INSERT INTO exports
                   (tenant_id,id,audit_id,format,object_backend,object_key,object_version_id,
                    content_sha256,size_bytes,signature_algorithm,signature_key_id,signature,
                    manifest,created_by,expires_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s) RETURNING *""",
                (
                    tenant_id,
                    identifier,
                    fields["audit_id"],
                    fields["format"],
                    fields["object_backend"],
                    fields["object_key"],
                    fields.get("object_version_id"),
                    fields["content_sha256"],
                    int(fields["size_bytes"]),
                    fields["signature_algorithm"],
                    fields["signature_key_id"],
                    fields["signature"],
                    encode(fields.get("manifest") or {}),
                    fields["created_by"],
                    expires_at,
                ),
            )
            row = dict(cursor.fetchone())
            self._activity(
                cursor, tenant_id, str(fields["audit_id"]), fields["created_by"],
                "export.created", "export", identifier,
                {
                    "format": fields["format"],
                    "content_sha256": fields["content_sha256"],
                    "signature_key_id": fields["signature_key_id"],
                },
            )
            return self._export_row(row)  # type: ignore[return-value]

    def get_export(self, tenant_id: str, export_id: str) -> dict:
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT * FROM exports WHERE tenant_id=%s AND id=%s", (tenant_id, export_id)
            )
            return self._export_row(self._one(cursor.fetchone(), "export"))  # type: ignore

    def list_exports(self, tenant_id: str, audit_id: str | None = None) -> list[dict]:
        with self._cursor() as cursor:
            if audit_id:
                cursor.execute(
                    """SELECT * FROM exports WHERE tenant_id=%s AND audit_id=%s
                        ORDER BY created_at DESC""",
                    (tenant_id, audit_id),
                )
            else:
                cursor.execute(
                    "SELECT * FROM exports WHERE tenant_id=%s ORDER BY created_at DESC LIMIT 200",
                    (tenant_id,),
                )
            return [self._export_row(row) for row in cursor.fetchall()]  # type: ignore[misc]

    @classmethod
    def _export_row(cls, row: dict | None) -> dict | None:
        if row is None:
            return None
        value = {key: _iso(item) for key, item in dict(row).items()}
        value["id"] = str(value["id"])
        value["audit_id"] = str(value["audit_id"])
        value["size_bytes"] = int(value["size_bytes"])
        value["manifest"] = (
            value["manifest"] if isinstance(value.get("manifest"), dict)
            else json.loads(value.get("manifest") or "{}")
        )
        return value

    # ------------------------------------------------------------------
    # telemetry
    # ------------------------------------------------------------------
    def record_telemetry(self, tenant_id: str, events: list[dict]) -> int:
        if not events:
            return 0
        rows = [
            (
                tenant_id,
                event.get("audit_id"),
                event.get("job_id"),
                str(event.get("stage", "unknown"))[:100],
                event.get("provider"),
                event.get("model"),
                str(event.get("outcome", "succeeded"))[:50],
                max(0, int(event.get("duration_ms", 0))),
                max(0, int(event.get("input_tokens", 0))),
                max(0, int(event.get("output_tokens", 0))),
                max(0, int(event.get("requests", 0))),
                round(float(event.get("cost_usd", 0.0)), 6),
                encode(event.get("attributes") or {}),
            )
            for event in events
        ]
        with self._cursor() as cursor:
            cursor.executemany(
                """INSERT INTO telemetry_events
                   (tenant_id,audit_id,job_id,stage,provider,model,outcome,duration_ms,
                    input_tokens,output_tokens,requests,cost_usd,attributes)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                rows,
            )
        return len(rows)

    def telemetry_window(
        self, tenant_id: str | None, *, hours: int = 168, audit_id: str | None = None
    ) -> list[dict]:
        clauses = ["created_at >= CURRENT_TIMESTAMP - make_interval(hours => %s)"]
        params: list[Any] = [int(max(1, hours))]
        if tenant_id:
            clauses.append("tenant_id=%s")
            params.append(tenant_id)
        if audit_id:
            clauses.append("audit_id=%s")
            params.append(audit_id)
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM telemetry_events WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC LIMIT 20000",
                tuple(params),
            )
            rows = []
            for row in cursor.fetchall():
                value = self._row(row)
                if value is None:
                    continue
                value["cost_usd"] = float(value.get("cost_usd") or 0.0)
                for key in ("audit_id", "job_id"):
                    if value.get(key) is not None:
                        value[key] = str(value[key])
                if not isinstance(value.get("attributes"), dict):
                    value["attributes"] = json.loads(value.get("attributes") or "{}")
                rows.append(value)
            return rows

    def audit_latency_window(self, tenant_id: str | None, *, hours: int = 168) -> list[dict]:
        """Queue wait and execution duration for completed jobs in the window."""
        clauses = ["j.created_at >= CURRENT_TIMESTAMP - make_interval(hours => %s)"]
        params: list[Any] = [int(max(1, hours))]
        if tenant_id:
            clauses.append("j.tenant_id=%s")
            params.append(tenant_id)
        with self._cursor() as cursor:
            cursor.execute(
                f"""SELECT j.tenant_id, j.id, j.audit_id, j.state, j.attempts,
                           EXTRACT(EPOCH FROM (j.started_at - j.created_at)) AS queue_wait_seconds,
                           EXTRACT(EPOCH FROM (j.completed_at - j.started_at)) AS run_seconds,
                           a.summary
                      FROM jobs j
                      LEFT JOIN audits a ON a.tenant_id=j.tenant_id AND a.id=j.audit_id
                     WHERE {' AND '.join(clauses)} AND j.completed_at IS NOT NULL
                     ORDER BY j.completed_at DESC LIMIT 5000""",
                tuple(params),
            )
            results = []
            for row in cursor.fetchall():
                value = dict(row)
                summary = value.get("summary")
                results.append(
                    {
                        "job_id": str(value["id"]),
                        "audit_id": str(value["audit_id"]),
                        "state": value["state"],
                        "attempts": int(value["attempts"]),
                        "queue_wait_seconds": float(value["queue_wait_seconds"] or 0.0),
                        "run_seconds": float(value["run_seconds"] or 0.0),
                        "summary": summary if isinstance(summary, dict) else {},
                    }
                )
            return results


__all__ = ["PostgresWorkspaceStore"]
