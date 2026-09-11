"""Phase 4 capabilities for the SQLite workspace.

The single-workstation profile keeps SQLite, but it must behave the same way as
the PostgreSQL runtime: leases and backoff on the queue, organizations and
membership, reviewer assignment, automatic repair records, signed export
metadata and cost/latency telemetry.

SQLite has no ``FOR UPDATE SKIP LOCKED``. ``BEGIN IMMEDIATE`` takes the database
write lock for the whole claim transaction, which gives the same single-winner
guarantee for the process counts this profile targets.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from src.workspace.common import (
    WorkspaceConflict,
    WorkspaceNotFound,
    decode,
    encode,
    utc_now,
)
from src.workspace.models import AuditState, JobState

TERMINAL_JOB_STATES = (
    JobState.SUCCEEDED.value,
    JobState.FAILED.value,
    JobState.CANCELLED.value,
    "dead_letter",
)

PHASE4_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_organizations (
    tenant_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    settings_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_members (
    tenant_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    display_name TEXT,
    email TEXT,
    roles_json TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, subject)
);
CREATE TABLE IF NOT EXISTS workspace_assignments (
    tenant_id TEXT NOT NULL,
    audit_id TEXT NOT NULL,
    finding_id TEXT NOT NULL,
    assignee TEXT NOT NULL,
    assigned_by TEXT NOT NULL,
    note TEXT,
    due_at TEXT,
    state TEXT NOT NULL DEFAULT 'assigned' CHECK(state IN ('assigned','released')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, audit_id, finding_id)
);
CREATE INDEX IF NOT EXISTS workspace_assignment_owner
  ON workspace_assignments (tenant_id, assignee, state);
CREATE TABLE IF NOT EXISTS workspace_repairs (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    audit_id TEXT NOT NULL,
    finding_id TEXT,
    issue_category TEXT NOT NULL,
    action TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN
      ('planned','queued','running','succeeded','failed','not_applicable')),
    attempt INTEGER NOT NULL DEFAULT 1,
    follow_up_job_id TEXT,
    follow_up_audit_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    requested_by TEXT NOT NULL,
    idempotency_key TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS workspace_repairs_audit
  ON workspace_repairs (tenant_id, audit_id, created_at DESC);
CREATE TABLE IF NOT EXISTS workspace_exports (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    audit_id TEXT NOT NULL,
    format TEXT NOT NULL CHECK(format IN ('pdf','html','json','bundle')),
    object_backend TEXT NOT NULL,
    object_key TEXT NOT NULL,
    object_version_id TEXT,
    content_sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    signature_algorithm TEXT NOT NULL,
    signature_key_id TEXT NOT NULL,
    signature TEXT NOT NULL,
    manifest_json TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS workspace_exports_audit
  ON workspace_exports (tenant_id, audit_id, created_at DESC);
CREATE TABLE IF NOT EXISTS workspace_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    audit_id TEXT,
    job_id TEXT,
    stage TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    outcome TEXT NOT NULL DEFAULT 'succeeded',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    requests INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS workspace_telemetry_window
  ON workspace_telemetry (tenant_id, created_at DESC);
"""

# (table, column, DDL type/default). Applied only when the column is absent.
PHASE4_COLUMNS = (
    ("workspace_manuscripts", "object_backend", "TEXT NOT NULL DEFAULT 'local'"),
    ("workspace_manuscripts", "object_key", "TEXT"),
    ("workspace_manuscripts", "object_version_id", "TEXT"),
    ("workspace_manuscripts", "text_layer_status", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("workspace_audits", "artifact_key", "TEXT"),
)

# The Phase 3 jobs table pinned its state CHECK to six values. The durable queue
# adds `dead_letter`, and SQLite cannot alter a CHECK in place, so the table is
# rebuilt once. Data, ordering and identifiers are preserved.
JOBS_TABLE_V2 = """
CREATE TABLE workspace_jobs_v2 (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    audit_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'audit'
        CHECK(kind IN ('audit','repair','export','evaluation')),
    state TEXT NOT NULL CHECK(state IN
        ('queued','running','succeeded','failed','cancel_requested','cancelled','dead_letter')),
    priority INTEGER NOT NULL DEFAULT 100 CHECK(priority BETWEEN 0 AND 1000),
    progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
    stage TEXT NOT NULL,
    request_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts >= 1),
    available_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00Z',
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
"""


def _plus_seconds(seconds: float) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=max(0.0, float(seconds))))
        .isoformat()
        .replace("+00:00", "Z")
    )


class SqliteWorkspacePhase4Mixin:
    """Durable-queue, organization, repair, export and telemetry operations."""

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------
    def _migrate_phase4(self, connection) -> None:
        connection.executescript(PHASE4_SCHEMA)
        for table, column, ddl in PHASE4_COLUMNS:
            existing = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        self._rebuild_jobs_table(connection)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS workspace_jobs_claimable "
            "ON workspace_jobs (state, priority, available_at, created_at)"
        )
        connection.execute(
            "INSERT OR IGNORE INTO workspace_schema(version, applied_at) VALUES(2, ?)",
            (utc_now(),),
        )

    @staticmethod
    def _rebuild_jobs_table(connection) -> None:
        """Add queue columns and the dead-letter state to the Phase 3 jobs table."""
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='workspace_jobs'"
        ).fetchone()
        if definition is None or "dead_letter" in (definition["sql"] or ""):
            return
        columns = [
            row["name"] for row in connection.execute("PRAGMA table_info(workspace_jobs)")
        ]
        carried = [name for name in columns if name in {
            "tenant_id", "id", "audit_id", "state", "progress", "stage", "request_json",
            "attempts", "lease_owner", "lease_expires_at", "cancel_requested", "error_code",
            "error_message", "result_json", "parent_job_id", "created_at", "started_at",
            "heartbeat_at", "completed_at",
        }]
        joined = ",".join(carried)
        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.executescript(JOBS_TABLE_V2)
            connection.execute(
                f"INSERT INTO workspace_jobs_v2 ({joined}) SELECT {joined} FROM workspace_jobs"
            )
            connection.execute("DROP TABLE workspace_jobs")
            connection.execute("ALTER TABLE workspace_jobs_v2 RENAME TO workspace_jobs")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys=ON")

    # ------------------------------------------------------------------
    # durable queue
    # ------------------------------------------------------------------
    def lease_next_job(
        self, worker_id: str, *, lease_seconds: int = 120, kinds: tuple[str, ...] = ("audit",)
    ) -> dict | None:
        now = utc_now()
        placeholders = ",".join("?" for _ in kinds)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"""SELECT * FROM workspace_jobs
                     WHERE state=? AND available_at<=? AND kind IN ({placeholders})
                     ORDER BY priority, available_at, created_at LIMIT 1""",
                (JobState.QUEUED.value, now, *kinds),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            job = dict(row)
            connection.execute(
                """UPDATE workspace_jobs SET state=?,stage='starting',
                       progress=MAX(progress,1),attempts=attempts+1,lease_owner=?,
                       lease_expires_at=?,heartbeat_at=?,started_at=COALESCE(started_at,?)
                   WHERE tenant_id=? AND id=?""",
                (
                    JobState.RUNNING.value,
                    worker_id[:200],
                    _plus_seconds(lease_seconds),
                    now,
                    now,
                    job["tenant_id"],
                    job["id"],
                ),
            )
            connection.execute(
                """UPDATE workspace_audits SET state=?,started_at=COALESCE(started_at,?)
                   WHERE tenant_id=? AND id=?""",
                (AuditState.PROCESSING.value, now, job["tenant_id"], job["audit_id"]),
            )
            self._activity(
                connection, job["tenant_id"], job["audit_id"], worker_id[:200], "audit.started",
                "job", job["id"], {"attempt": job["attempts"] + 1},
            )
            refreshed = dict(
                connection.execute(
                    "SELECT * FROM workspace_jobs WHERE tenant_id=? AND id=?",
                    (job["tenant_id"], job["id"]),
                ).fetchone()
            )
            connection.commit()
            return refreshed

    def heartbeat_job(
        self, tenant_id: str, job_id: str, worker_id: str, *, lease_seconds: int = 120
    ) -> bool:
        with self.connection() as connection:
            changed = connection.execute(
                """UPDATE workspace_jobs SET heartbeat_at=?,lease_expires_at=?
                   WHERE tenant_id=? AND id=? AND lease_owner=? AND state IN (?,?)""",
                (
                    utc_now(),
                    _plus_seconds(lease_seconds),
                    tenant_id,
                    job_id,
                    worker_id[:200],
                    JobState.RUNNING.value,
                    JobState.CANCEL_REQUESTED.value,
                ),
            ).rowcount
            return bool(changed)

    def reclaim_expired_leases(self) -> int:
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = [
                dict(row)
                for row in connection.execute(
                    """SELECT tenant_id,id,audit_id,attempts,max_attempts FROM workspace_jobs
                        WHERE state IN (?,?) AND lease_expires_at IS NOT NULL
                          AND lease_expires_at < ?""",
                    (JobState.RUNNING.value, JobState.CANCEL_REQUESTED.value, now),
                ).fetchall()
            ]
            for row in rows:
                exhausted = int(row["attempts"]) >= int(row["max_attempts"])
                next_state = "dead_letter" if exhausted else JobState.QUEUED.value
                connection.execute(
                    """UPDATE workspace_jobs SET state=?,stage=?,lease_owner=NULL,
                           lease_expires_at=NULL,available_at=?,
                           error_code=COALESCE(?,error_code),
                           error_message=COALESCE(?,error_message),
                           completed_at=COALESCE(?,completed_at)
                       WHERE tenant_id=? AND id=?""",
                    (
                        next_state,
                        "dead_letter" if exhausted else "recovered_after_lease_expiry",
                        now,
                        "lease_expired" if exhausted else None,
                        (
                            "The worker stopped responding and the retry budget is exhausted."
                            if exhausted
                            else None
                        ),
                        now if exhausted else None,
                        row["tenant_id"],
                        row["id"],
                    ),
                )
                connection.execute(
                    "UPDATE workspace_audits SET state=? WHERE tenant_id=? AND id=?",
                    (
                        AuditState.FAILED.value if exhausted else AuditState.QUEUED.value,
                        row["tenant_id"],
                        row["audit_id"],
                    ),
                )
                self._activity(
                    connection, row["tenant_id"], row["audit_id"], "queue-supervisor",
                    "job.lease_expired", "job", row["id"], {"next_state": next_state},
                )
            connection.commit()
            return len(rows)

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
        del max_attempts
        row = self.get_job(tenant_id, job_id)
        if int(row["attempts"]) >= int(row.get("max_attempts") or 3) or row["cancel_requested"]:
            self.finish_job(
                tenant_id,
                job_id,
                success=False,
                error_code=error_code,
                error_message=error_message,
            )
            return "failed"
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE workspace_jobs SET state=?,stage='retry_scheduled',lease_owner=NULL,
                       lease_expires_at=NULL,available_at=?,error_code=?,error_message=?
                   WHERE tenant_id=? AND id=?""",
                (
                    JobState.QUEUED.value,
                    _plus_seconds(backoff_seconds),
                    error_code,
                    error_message,
                    tenant_id,
                    job_id,
                ),
            )
            connection.execute(
                "UPDATE workspace_audits SET state=? WHERE tenant_id=? AND id=?",
                (AuditState.QUEUED.value, tenant_id, row["audit_id"]),
            )
            self._activity(
                connection, tenant_id, row["audit_id"], worker_id[:200], "job.retry_scheduled",
                "job", job_id, {"attempt": row["attempts"], "error_code": error_code},
            )
            connection.commit()
        return "requeued"

    def queue_depth(self, tenant_id: str | None = None) -> dict:
        with self.connection() as connection:
            if tenant_id:
                rows = connection.execute(
                    "SELECT state, COUNT(*) total FROM workspace_jobs WHERE tenant_id=? "
                    "GROUP BY state",
                    (tenant_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT state, COUNT(*) total FROM workspace_jobs GROUP BY state"
                ).fetchall()
            counts = {row["state"]: int(row["total"]) for row in rows}
            oldest_row = connection.execute(
                "SELECT MIN(created_at) oldest FROM workspace_jobs WHERE state=?",
                (JobState.QUEUED.value,),
            ).fetchone()
        oldest_seconds = 0.0
        oldest = (oldest_row or {})["oldest"] if oldest_row else None
        if oldest:
            try:
                started = datetime.fromisoformat(str(oldest).replace("Z", "+00:00"))
                oldest_seconds = max(
                    0.0, (datetime.now(timezone.utc) - started).total_seconds()
                )
            except ValueError:
                oldest_seconds = 0.0
        return {
            "counts": counts,
            "queued": counts.get(JobState.QUEUED.value, 0),
            "running": counts.get(JobState.RUNNING.value, 0),
            "dead_letter": counts.get("dead_letter", 0),
            "oldest_queued_seconds": round(oldest_seconds, 2),
        }

    # ------------------------------------------------------------------
    # organizations and membership
    # ------------------------------------------------------------------
    def ensure_organization(self, tenant_id: str, name: str, *, actor_id: str = "system") -> dict:
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM workspace_organizations WHERE tenant_id=?", (tenant_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO workspace_organizations
                       (tenant_id,name,settings_json,created_at,updated_at)
                       VALUES(?,?,'{}',?,?)""",
                    (tenant_id, (name or "Organization")[:200], now, now),
                )
                self._activity(
                    connection, tenant_id, None, actor_id, "organization.created",
                    "organization", tenant_id, {"name": (name or "Organization")[:200]},
                )
            row = dict(
                connection.execute(
                    "SELECT * FROM workspace_organizations WHERE tenant_id=?", (tenant_id,)
                ).fetchone()
            )
            connection.commit()
        row["settings"] = decode(row.pop("settings_json", "{}"), {})
        return row

    def get_organization(self, tenant_id: str) -> dict | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM workspace_organizations WHERE tenant_id=?", (tenant_id,)
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["settings"] = decode(value.pop("settings_json", "{}"), {})
        return value

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
        now = utc_now()
        cleaned = sorted({str(role).strip().lower() for role in roles if str(role).strip()})
        self.ensure_organization(tenant_id, "Organization", actor_id=actor_id)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO workspace_members
                   (tenant_id,subject,display_name,email,roles_json,active,created_at,updated_at)
                   VALUES(?,?,?,?,?,1,?,?)
                   ON CONFLICT(tenant_id,subject) DO UPDATE SET
                     display_name=COALESCE(excluded.display_name,workspace_members.display_name),
                     email=COALESCE(excluded.email,workspace_members.email),
                     roles_json=excluded.roles_json, active=1, updated_at=excluded.updated_at""",
                (tenant_id, subject[:255], display_name, email, encode(cleaned), now, now),
            )
            self._activity(
                connection, tenant_id, None, actor_id, "member.upserted", "member",
                subject[:255], {"roles": cleaned},
            )
            row = dict(
                connection.execute(
                    "SELECT * FROM workspace_members WHERE tenant_id=? AND subject=?",
                    (tenant_id, subject[:255]),
                ).fetchone()
            )
            connection.commit()
        return self._member_row(row)

    def list_members(self, tenant_id: str) -> list[dict]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM workspace_members WHERE tenant_id=? ORDER BY subject",
                (tenant_id,),
            ).fetchall()
        return [self._member_row(dict(row)) for row in rows]

    def deactivate_member(self, tenant_id: str, subject: str, actor_id: str) -> dict:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE workspace_members SET active=0,updated_at=? WHERE tenant_id=? AND subject=?",
                (utc_now(), tenant_id, subject),
            ).rowcount
            if not changed:
                connection.rollback()
                raise WorkspaceNotFound("member was not found")
            self._activity(
                connection, tenant_id, None, actor_id, "member.deactivated", "member", subject, {}
            )
            row = dict(
                connection.execute(
                    "SELECT * FROM workspace_members WHERE tenant_id=? AND subject=?",
                    (tenant_id, subject),
                ).fetchone()
            )
            connection.commit()
        return self._member_row(row)

    @staticmethod
    def _member_row(row: dict) -> dict:
        value = dict(row)
        value["roles"] = decode(value.pop("roles_json", "[]"), [])
        value["active"] = bool(value.get("active"))
        return value

    # ------------------------------------------------------------------
    # manuscripts
    # ------------------------------------------------------------------
    def update_manuscript_text_layer(
        self, tenant_id: str, manuscript_id: str, status: str
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """UPDATE workspace_manuscripts SET text_layer_status=?,updated_at=?
                   WHERE tenant_id=? AND id=?""",
                (status, utc_now(), tenant_id, manuscript_id),
            )

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
        now = utc_now()
        self.get_audit(tenant_id, audit_id)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            member = connection.execute(
                "SELECT active FROM workspace_members WHERE tenant_id=? AND subject=?",
                (tenant_id, assignee),
            ).fetchone()
            if member is not None and not member["active"]:
                connection.rollback()
                raise WorkspaceConflict("the assignee is not an active member of this organization")
            connection.execute(
                """INSERT INTO workspace_assignments
                   (tenant_id,audit_id,finding_id,assignee,assigned_by,note,due_at,state,
                    created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,'assigned',?,?)
                   ON CONFLICT(tenant_id,audit_id,finding_id) DO UPDATE SET
                     assignee=excluded.assignee, assigned_by=excluded.assigned_by,
                     note=excluded.note, due_at=excluded.due_at, state='assigned',
                     updated_at=excluded.updated_at""",
                (
                    tenant_id,
                    audit_id,
                    finding_id,
                    assignee[:255],
                    actor_id,
                    note,
                    due_at,
                    now,
                    now,
                ),
            )
            self._activity(
                connection, tenant_id, audit_id, actor_id, "case.assigned", "finding",
                finding_id, {"assignee": assignee[:255]},
            )
            row = dict(
                connection.execute(
                    """SELECT * FROM workspace_assignments
                        WHERE tenant_id=? AND audit_id=? AND finding_id=?""",
                    (tenant_id, audit_id, finding_id),
                ).fetchone()
            )
            connection.commit()
        return row

    def unassign_case(self, tenant_id: str, audit_id: str, finding_id: str, actor_id: str) -> dict:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE workspace_assignments SET state='released',updated_at=?
                   WHERE tenant_id=? AND audit_id=? AND finding_id=?""",
                (utc_now(), tenant_id, audit_id, finding_id),
            ).rowcount
            if not changed:
                connection.rollback()
                raise WorkspaceNotFound("assignment was not found")
            self._activity(
                connection, tenant_id, audit_id, actor_id, "case.released", "finding",
                finding_id, {},
            )
            row = dict(
                connection.execute(
                    """SELECT * FROM workspace_assignments
                        WHERE tenant_id=? AND audit_id=? AND finding_id=?""",
                    (tenant_id, audit_id, finding_id),
                ).fetchone()
            )
            connection.commit()
        return row

    def list_assignments(
        self, tenant_id: str, *, audit_id: str | None = None, assignee: str | None = None
    ) -> list[dict]:
        clauses, params = ["tenant_id=?"], [tenant_id]
        if audit_id:
            clauses.append("audit_id=?")
            params.append(audit_id)
        if assignee:
            clauses.append("assignee=?")
            params.append(assignee)
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM workspace_assignments WHERE {' AND '.join(clauses)} "
                "ORDER BY updated_at DESC",
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def assignment_workload(self, tenant_id: str) -> list[dict]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT m.subject, m.display_name, m.roles_json, m.active,
                          COALESCE(counts.open_cases,0) open_cases
                     FROM workspace_members m
                     LEFT JOIN (SELECT assignee, COUNT(*) open_cases FROM workspace_assignments
                                WHERE tenant_id=? AND state='assigned' GROUP BY assignee) counts
                       ON counts.assignee = m.subject
                    WHERE m.tenant_id=? ORDER BY open_cases ASC, m.subject ASC""",
                (tenant_id, tenant_id),
            ).fetchall()
        return [self._member_row(dict(row)) for row in rows]

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
        identifier, now = self._id(), utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                replay = connection.execute(
                    "SELECT * FROM workspace_repairs WHERE tenant_id=? AND idempotency_key=?",
                    (tenant_id, idempotency_key),
                ).fetchone()
                if replay:
                    connection.commit()
                    return self._repair_row(dict(replay))
            connection.execute(
                """INSERT INTO workspace_repairs
                   (tenant_id,id,audit_id,finding_id,issue_category,action,state,attempt,
                    follow_up_job_id,follow_up_audit_id,detail_json,requested_by,
                    idempotency_key,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    now,
                ),
            )
            self._activity(
                connection, tenant_id, audit_id, requested_by, f"repair.{state}", "repair",
                identifier, {"issue_category": issue_category, "action": action},
            )
            row = dict(
                connection.execute(
                    "SELECT * FROM workspace_repairs WHERE tenant_id=? AND id=?",
                    (tenant_id, identifier),
                ).fetchone()
            )
            connection.commit()
        return self._repair_row(row)

    def update_repair(self, tenant_id: str, repair_id: str, **fields: Any) -> dict:
        allowed = {"state", "detail", "follow_up_job_id", "follow_up_audit_id", "attempt"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_repair(tenant_id, repair_id)
        assignments, params = [], []
        for key, value in updates.items():
            if key == "detail":
                assignments.append("detail_json=?")
                params.append(encode(value))
            else:
                assignments.append(f"{key}=?")
                params.append(value)
        if updates.get("state") in {"succeeded", "failed", "not_applicable"}:
            assignments.append("completed_at=?")
            params.append(utc_now())
        params.extend([tenant_id, repair_id])
        with self.connection() as connection:
            changed = connection.execute(
                f"UPDATE workspace_repairs SET {', '.join(assignments)} "
                "WHERE tenant_id=? AND id=?",
                tuple(params),
            ).rowcount
            if not changed:
                raise WorkspaceNotFound("repair was not found")
        return self.get_repair(tenant_id, repair_id)

    def get_repair(self, tenant_id: str, repair_id: str) -> dict:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM workspace_repairs WHERE tenant_id=? AND id=?",
                (tenant_id, repair_id),
            ).fetchone()
        if row is None:
            raise WorkspaceNotFound("repair was not found")
        return self._repair_row(dict(row))

    def list_repairs(self, tenant_id: str, audit_id: str | None = None) -> list[dict]:
        with self.connection() as connection:
            if audit_id:
                rows = connection.execute(
                    """SELECT * FROM workspace_repairs WHERE tenant_id=? AND audit_id=?
                        ORDER BY created_at DESC""",
                    (tenant_id, audit_id),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM workspace_repairs WHERE tenant_id=? "
                    "ORDER BY created_at DESC LIMIT 200",
                    (tenant_id,),
                ).fetchall()
        return [self._repair_row(dict(row)) for row in rows]

    @staticmethod
    def _repair_row(row: dict) -> dict:
        value = dict(row)
        value["detail"] = decode(value.pop("detail_json", "{}"), {})
        return value

    # ------------------------------------------------------------------
    # exports
    # ------------------------------------------------------------------
    def record_export(self, tenant_id: str, **fields: Any) -> dict:
        identifier, now = self._id(), utc_now()
        expires_at = None
        retention_days = int(fields.get("retention_days") or 0)
        if retention_days > 0:
            expires_at = _plus_seconds(retention_days * 86400)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO workspace_exports
                   (tenant_id,id,audit_id,format,object_backend,object_key,object_version_id,
                    content_sha256,size_bytes,signature_algorithm,signature_key_id,signature,
                    manifest_json,created_by,expires_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    now,
                ),
            )
            self._activity(
                connection, tenant_id, fields["audit_id"], fields["created_by"], "export.created",
                "export", identifier,
                {
                    "format": fields["format"],
                    "content_sha256": fields["content_sha256"],
                    "signature_key_id": fields["signature_key_id"],
                },
            )
            row = dict(
                connection.execute(
                    "SELECT * FROM workspace_exports WHERE tenant_id=? AND id=?",
                    (tenant_id, identifier),
                ).fetchone()
            )
            connection.commit()
        return self._export_row(row)

    def get_export(self, tenant_id: str, export_id: str) -> dict:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM workspace_exports WHERE tenant_id=? AND id=?",
                (tenant_id, export_id),
            ).fetchone()
        if row is None:
            raise WorkspaceNotFound("export was not found")
        return self._export_row(dict(row))

    def list_exports(self, tenant_id: str, audit_id: str | None = None) -> list[dict]:
        with self.connection() as connection:
            if audit_id:
                rows = connection.execute(
                    """SELECT * FROM workspace_exports WHERE tenant_id=? AND audit_id=?
                        ORDER BY created_at DESC""",
                    (tenant_id, audit_id),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM workspace_exports WHERE tenant_id=? "
                    "ORDER BY created_at DESC LIMIT 200",
                    (tenant_id,),
                ).fetchall()
        return [self._export_row(dict(row)) for row in rows]

    @staticmethod
    def _export_row(row: dict) -> dict:
        value = dict(row)
        value["manifest"] = decode(value.pop("manifest_json", "{}"), {})
        value["size_bytes"] = int(value.get("size_bytes") or 0)
        return value

    # ------------------------------------------------------------------
    # telemetry
    # ------------------------------------------------------------------
    def record_telemetry(self, tenant_id: str, events: list[dict]) -> int:
        if not events:
            return 0
        now = utc_now()
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
                now,
            )
            for event in events
        ]
        with self.connection() as connection:
            connection.executemany(
                """INSERT INTO workspace_telemetry
                   (tenant_id,audit_id,job_id,stage,provider,model,outcome,duration_ms,
                    input_tokens,output_tokens,requests,cost_usd,attributes_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
        return len(rows)

    def telemetry_window(
        self, tenant_id: str | None, *, hours: int = 168, audit_id: str | None = None
    ) -> list[dict]:
        cutoff = (
            (datetime.now(timezone.utc) - timedelta(hours=int(max(1, hours))))
            .isoformat()
            .replace("+00:00", "Z")
        )
        clauses, params = ["created_at>=?"], [cutoff]
        if tenant_id:
            clauses.append("tenant_id=?")
            params.append(tenant_id)
        if audit_id:
            clauses.append("audit_id=?")
            params.append(audit_id)
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM workspace_telemetry WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC LIMIT 20000",
                tuple(params),
            ).fetchall()
        results = []
        for row in rows:
            value = dict(row)
            value["attributes"] = decode(value.pop("attributes_json", "{}"), {})
            value["cost_usd"] = float(value.get("cost_usd") or 0.0)
            results.append(value)
        return results

    def audit_latency_window(self, tenant_id: str | None, *, hours: int = 168) -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=int(max(1, hours)))
        clauses, params = ["j.completed_at IS NOT NULL"], []
        if tenant_id:
            clauses.append("j.tenant_id=?")
            params.append(tenant_id)
        with self.connection() as connection:
            rows = connection.execute(
                f"""SELECT j.id, j.audit_id, j.state, j.attempts, j.created_at, j.started_at,
                           j.completed_at, a.summary_json
                      FROM workspace_jobs j
                      LEFT JOIN workspace_audits a
                        ON a.tenant_id=j.tenant_id AND a.id=j.audit_id
                     WHERE {' AND '.join(clauses)}
                     ORDER BY j.completed_at DESC LIMIT 5000""",
                tuple(params),
            ).fetchall()

        def moment(value):
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None

        results = []
        for row in rows:
            value = dict(row)
            created, started, completed = (
                moment(value["created_at"]),
                moment(value["started_at"]),
                moment(value["completed_at"]),
            )
            if completed is None or completed < cutoff:
                continue
            results.append(
                {
                    "job_id": value["id"],
                    "audit_id": value["audit_id"],
                    "state": value["state"],
                    "attempts": int(value["attempts"]),
                    "queue_wait_seconds": (
                        max(0.0, (started - created).total_seconds())
                        if started and created
                        else 0.0
                    ),
                    "run_seconds": (
                        max(0.0, (completed - started).total_seconds()) if started else 0.0
                    ),
                    "summary": json.loads(value.get("summary_json") or "{}"),
                }
            )
        return results


__all__ = ["SqliteWorkspacePhase4Mixin", "TERMINAL_JOB_STATES"]
