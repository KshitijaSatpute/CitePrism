"""Typed append-only PostgreSQL adapter with tenant-scoped transactions."""
from contextlib import contextmanager
from enum import Enum

from src.trust.audit import ZERO_HASH, build_audit_event
from src.trust.canonical import canonical_object, sha256_json
from src.trust.models import AuditEvent
from src.trust.ports import TrustConfigurationError, TrustConflictError
from src.trust.schema import MODELS, RUNTIME_TABLES, kind_of


def driver():
    try:
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
    except ImportError as exc:
        raise TrustConfigurationError("install psycopg[binary] for PostgreSQL") from exc
    return psycopg, dict_row, Jsonb


class PostgresTrustRepository:
    def __init__(self, dsn, *, expected_role=None):
        if not dsn or not dsn.startswith(("postgresql://", "postgres://")):
            raise TrustConfigurationError("a PostgreSQL runtime DSN is required")
        self.dsn, self.expected_role = dsn, expected_role

    def _assert_runtime(self, cursor):
        cursor.execute("""
            SELECT current_user AS name, rolcanlogin,
                   rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole
                   OR rolreplication OR rolinherit OR EXISTS (
                       SELECT 1 FROM pg_auth_members WHERE member = pg_roles.oid
                   ) AS privileged
            FROM pg_roles WHERE rolname = current_user
        """)
        role = cursor.fetchone()
        if not role or not role["rolcanlogin"] or role["privileged"]:
            raise TrustConfigurationError("runtime must use an unprivileged LOGIN NOINHERIT role with no memberships")
        if self.expected_role and role["name"] != self.expected_role:
            raise TrustConfigurationError("connected role differs from expected runtime role")
        cursor.execute("""
            SELECT count(*) AS table_count,
                COALESCE(bool_and(has_table_privilege(current_user, c.oid, 'SELECT, INSERT')), false) AS usable,
                COALESCE(bool_or(
                    pg_has_role(current_user, c.relowner, 'MEMBER')
                    OR has_schema_privilege(current_user, n.oid, 'CREATE')
                    OR has_table_privilege(current_user, c.oid, 'UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
                    OR NOT c.relrowsecurity OR NOT c.relforcerowsecurity
                ), true) AS unsafe
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'citeprism' AND c.relkind = 'r' AND c.relname = ANY(%s)
        """, (list(RUNTIME_TABLES),))
        permissions = cursor.fetchone()
        # has_table_privilege with a comma list is OR, so check each required permission separately.
        cursor.execute("""
            SELECT COALESCE(bool_and(
                has_table_privilege(current_user, c.oid, 'SELECT') AND
                has_table_privilege(current_user, c.oid, 'INSERT')
            ), false) AS required
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'citeprism' AND c.relkind = 'r' AND c.relname = ANY(%s)
        """, (list(RUNTIME_TABLES),))
        if permissions["table_count"] != len(RUNTIME_TABLES) or permissions["unsafe"] or not cursor.fetchone()["required"]:
            raise TrustConfigurationError("trust schema/permissions unsafe or incomplete; run migrations with the administrator")

    @contextmanager
    def _transaction(self, tenant_id, *, write=False, snapshot=False):
        psycopg, dict_row, _ = driver()
        with psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=10) as connection:
            with connection.cursor() as cursor:
                if snapshot:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                self._assert_runtime(cursor)
                cursor.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
                cursor.execute("SET LOCAL statement_timeout = '30s'")
                if write:
                    cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(tenant_id),))
                    cursor.execute("INSERT INTO citeprism.tenants(id) VALUES (%s) ON CONFLICT DO NOTHING", (tenant_id,))
                yield cursor

    @staticmethod
    def _insert(cursor, table, record, *, digest=None):
        _, _, Jsonb = driver()
        values = record.model_dump(mode="python")
        if digest is not None:
            values["record_hash"] = digest
        parameters = []
        for value in values.values():
            if isinstance(value, dict):
                value = Jsonb(canonical_object(value))
            elif isinstance(value, Enum):
                value = value.value
            parameters.append(value)
        # table and columns are exclusively from the static model allowlist.
        columns = ", ".join(values)
        placeholders = ", ".join(["%s"] * len(values))
        cursor.execute(f"INSERT INTO citeprism.{table} ({columns}) VALUES ({placeholders})", parameters)

    def append_batch(self, records, actor):
        if not records:
            return []
        tenant = records[0].tenant_id
        if any(r.tenant_id != tenant for r in records):
            raise TrustConflictError("a batch must belong to one tenant")
        psycopg, _, _ = driver()
        try:
            with self._transaction(tenant, write=True) as cursor:
                cursor.execute("SELECT sequence_number, event_hash FROM citeprism.audit_events WHERE tenant_id = %s ORDER BY sequence_number DESC LIMIT 1", (tenant,))
                previous = cursor.fetchone()
                sequence = previous["sequence_number"] if previous else 0
                head = previous["event_hash"] if previous else ZERO_HASH
                created = []
                for item in records:
                    kind = kind_of(item)
                    table, model = MODELS[kind]
                    record = model.model_validate(item.model_dump(mode="json"))
                    digest = sha256_json(record)
                    cursor.execute(f"SELECT * FROM citeprism.{table} WHERE tenant_id = %s AND id = %s", (tenant, record.id))
                    existing = cursor.fetchone()
                    if existing:
                        stored = existing.pop("record_hash")
                        if digest != stored or sha256_json(model.model_validate(existing)) != digest:
                            raise TrustConflictError("immutable identifier reused for different content")
                        created.append(False)
                        continue
                    self._insert(cursor, table, record, digest=digest)
                    sequence += 1
                    event = build_audit_event(
                        tenant_id=tenant, sequence_number=sequence, previous_hash=head,
                        actor=actor, action=f"{kind}.recorded", aggregate_type=kind,
                        aggregate_id=record.id, payload={"record_hash": digest, "record_version": 1},
                    )
                    self._insert(cursor, "audit_events", event)
                    head = event.event_hash
                    created.append(True)
                return created
        except psycopg.IntegrityError as exc:
            raise TrustConflictError("trust record conflicts with a schema/provenance constraint") from exc

    def get_record(self, kind, tenant_id, record_id):
        table, model = MODELS[kind]
        with self._transaction(tenant_id) as cursor:
            cursor.execute(f"SELECT * FROM citeprism.{table} WHERE tenant_id = %s AND id = %s", (tenant_id, record_id))
            row = cursor.fetchone()
            if row is None:
                return None
            row.pop("record_hash")
            return model.model_validate(row)

    def get_document_version_by_id(self, tenant_id, version_id):
        return self.get_record("document_version", tenant_id, version_id)

    def _find_version(self, tenant_id, where, value):
        with self._transaction(tenant_id) as cursor:
            cursor.execute(f"""
                SELECT to_jsonb(d) AS document, to_jsonb(v) AS version
                FROM citeprism.documents d JOIN citeprism.document_versions v
                  ON d.tenant_id = v.tenant_id AND d.id = v.document_id
                WHERE d.tenant_id = %s AND {where} = %s
                ORDER BY v.version_number DESC LIMIT 1
            """, (tenant_id, value))
            row = cursor.fetchone()
            if row is None:
                return None
            for part in ("document", "version"):
                row[part].pop("record_hash")
            return (MODELS["document"][1].model_validate(row["document"]),
                    MODELS["document_version"][1].model_validate(row["version"]))

    def get_document_version_by_hash(self, tenant_id, digest):
        return self._find_version(tenant_id, "v.content_sha256", digest)

    def get_latest_document_version_by_external_key(self, tenant_id, key):
        return self._find_version(tenant_id, "d.external_key", key)

    def audit_snapshot(self, tenant_id):
        with self._transaction(tenant_id, snapshot=True) as cursor:
            cursor.execute("SELECT * FROM citeprism.audit_events WHERE tenant_id = %s ORDER BY sequence_number", (tenant_id,))
            events = [AuditEvent.model_validate(row) for row in cursor.fetchall()]
            records = {}
            for kind, (table, model) in MODELS.items():
                cursor.execute(f"SELECT * FROM citeprism.{table} WHERE tenant_id = %s", (tenant_id,))
                for row in cursor.fetchall():
                    stored = row.pop("record_hash")
                    record = model.model_validate(row)
                    records[(kind, record.id)] = (record, stored)
            return events, records
