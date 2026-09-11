"""Checksum-guarded workspace migrations with explicit runtime grants.

The workspace schema owns mutable operational state, so the runtime role needs
UPDATE on most tables. It never receives UPDATE or DELETE on the append-only
`activity` and `decisions` tables, and it is never the migration role.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from src.trust.ports import TrustConfigurationError, TrustIntegrityError

SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

# Tables whose rows the application updates in place.
MUTABLE_TABLES = (
    "organizations",
    "organization_members",
    "manuscripts",
    "audits",
    "jobs",
    "corrections",
    "assignments",
    "repairs",
    "exports",
)
# Append-only tables: INSERT and SELECT only.
APPEND_ONLY_TABLES = ("activity", "decisions", "telemetry_events")


def default_migrations_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "database" / "workspace_migrations"


def _driver():
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise TrustConfigurationError(
            "install psycopg to use the PostgreSQL workspace backend"
        ) from exc
    return psycopg


def render(source: str, schema: str) -> str:
    """Substitute the validated schema identifier into a migration."""
    if not SCHEMA_NAME.fullmatch(schema):
        raise TrustConfigurationError("workspace schema name is not a safe identifier")
    return source.replace("${SCHEMA}", schema)


def apply_workspace_migrations(
    dsn: str,
    *,
    schema: str = "citeprism_workspace",
    runtime_role: str | None = None,
    migrations_dir: str | Path | None = None,
) -> list[str]:
    """Apply pending workspace migrations and (re)assert least-privilege grants."""
    if not dsn or not dsn.startswith(("postgresql://", "postgres://")):
        raise TrustConfigurationError("a PostgreSQL migration DSN is required")
    if not SCHEMA_NAME.fullmatch(schema):
        raise TrustConfigurationError("workspace schema name is not a safe identifier")

    psycopg = _driver()
    from psycopg import sql

    paths = sorted(Path(migrations_dir or default_migrations_dir()).glob("[0-9][0-9][0-9]_*.sql"))
    if not paths:
        raise TrustConfigurationError("no workspace migrations found")

    applied: list[str] = []
    with psycopg.connect(dsn, connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '30s'")
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"citeprism:workspace-migrations:{schema}",),
            )
            cursor.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema))
            )
            cursor.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {}.schema_migrations (
                        version TEXT PRIMARY KEY,
                        checksum_sha256 TEXT NOT NULL,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                ).format(sql.Identifier(schema))
            )
            for path in paths:
                source = path.read_text(encoding="utf-8")
                digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
                cursor.execute(
                    sql.SQL("SELECT checksum_sha256 FROM {}.schema_migrations WHERE version = %s")
                    .format(sql.Identifier(schema)),
                    (path.name,),
                )
                existing = cursor.fetchone()
                if existing:
                    if existing[0] != digest:
                        raise TrustIntegrityError(f"workspace migration drift: {path.name}")
                    continue
                cursor.execute(render(source, schema))
                cursor.execute(
                    sql.SQL(
                        "INSERT INTO {}.schema_migrations(version, checksum_sha256) VALUES (%s, %s)"
                    ).format(sql.Identifier(schema)),
                    (path.name, digest),
                )
                applied.append(path.name)

            if runtime_role and runtime_role.strip():
                _grant(cursor, sql, schema, runtime_role.strip())
    return applied


def _grant(cursor, sql, schema: str, runtime_role: str) -> None:
    cursor.execute(
        """
        SELECT rolcanlogin, rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole
             OR rolreplication
        FROM pg_roles WHERE rolname = %s
        """,
        (runtime_role,),
    )
    attributes = cursor.fetchone()
    if not attributes or not attributes[0] or attributes[1]:
        raise TrustConfigurationError(
            "the workspace runtime role must exist and hold no superuser-class attributes"
        )
    role = sql.Identifier(runtime_role)
    schema_id = sql.Identifier(schema)
    cursor.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(schema_id))
    cursor.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM PUBLIC").format(schema_id))
    cursor.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(schema_id, role))
    cursor.execute(
        sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(schema_id, role)
    )
    cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema_id, role))
    for table in MUTABLE_TABLES:
        cursor.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE ON {}.{} TO {}").format(
                schema_id, sql.Identifier(table), role
            )
        )
    for table in APPEND_ONLY_TABLES:
        cursor.execute(
            sql.SQL("GRANT SELECT, INSERT ON {}.{} TO {}").format(
                schema_id, sql.Identifier(table), role
            )
        )
    for table in ("activity", "telemetry_events"):
        cursor.execute(
            sql.SQL("GRANT USAGE ON SEQUENCE {}.{} TO {}").format(
                schema_id, sql.Identifier(f"{table}_id_seq"), role
            )
        )
    cursor.execute(
        sql.SQL("GRANT SELECT ON {}.schema_migrations TO {}").format(schema_id, role)
    )


__all__ = ["apply_workspace_migrations", "default_migrations_dir", "render"]
