"""Serialized, checksum-guarded migrations and explicit runtime grants."""
import hashlib
from pathlib import Path

from src.trust.ports import TrustConfigurationError, TrustIntegrityError
from src.trust.postgres_repository import driver
from src.trust.schema import RUNTIME_TABLES


def default_migrations_dir():
    return Path(__file__).resolve().parents[2] / "database" / "migrations"


def apply_migrations(dsn, migrations_dir=None, *, runtime_role=None):
    if not dsn or not dsn.startswith(("postgresql://", "postgres://")):
        raise TrustConfigurationError("a separate PostgreSQL migration DSN is required")
    if not runtime_role or not runtime_role.strip():
        raise TrustConfigurationError("an existing runtime role is required")
    psycopg, _, _ = driver()
    from psycopg import sql

    paths = sorted(Path(migrations_dir or default_migrations_dir()).glob("[0-9][0-9][0-9]_*.sql"))
    if not paths:
        raise TrustConfigurationError("no trust migrations found")
    applied = []
    with psycopg.connect(dsn, connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '30s'")
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended('citeprism:migrations', 0))")
            cursor.execute("""
                SELECT rolcanlogin, rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole
                    OR rolreplication OR rolinherit OR EXISTS (
                        SELECT 1 FROM pg_auth_members WHERE member = pg_roles.oid
                    ) FROM pg_roles WHERE rolname = %s
            """, (runtime_role,))
            attributes = cursor.fetchone()
            if not attributes or not attributes[0] or attributes[1]:
                raise TrustConfigurationError("runtime must exist and use unprivileged LOGIN NOINHERIT with no memberships")
            cursor.execute("CREATE SCHEMA IF NOT EXISTS citeprism")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS citeprism.schema_migrations (
                    version TEXT PRIMARY KEY, checksum_sha256 TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            for path in paths:
                source = path.read_text(encoding="utf-8")  # newline-normalized across Windows/Linux
                digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
                cursor.execute("SELECT checksum_sha256 FROM citeprism.schema_migrations WHERE version = %s", (path.name,))
                existing = cursor.fetchone()
                if existing:
                    if existing[0] != digest:
                        raise TrustIntegrityError(f"migration checksum drift: {path.name}")
                    continue
                cursor.execute(source)
                cursor.execute("INSERT INTO citeprism.schema_migrations(version, checksum_sha256) VALUES (%s, %s)", (path.name, digest))
                applied.append(path.name)
            role = sql.Identifier(runtime_role)
            cursor.execute("REVOKE ALL ON SCHEMA citeprism FROM PUBLIC")
            cursor.execute("REVOKE ALL ON ALL TABLES IN SCHEMA citeprism FROM PUBLIC")
            cursor.execute(sql.SQL("REVOKE ALL ON SCHEMA citeprism FROM {}").format(role))
            cursor.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA citeprism FROM {}").format(role))
            cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA citeprism TO {}").format(role))
            for table in RUNTIME_TABLES:
                cursor.execute(sql.SQL("GRANT SELECT, INSERT ON citeprism.{} TO {}").format(sql.Identifier(table), role))
    return applied
