"""Process-local reference repository; never a production database."""
from threading import RLock

from src.trust.audit import ZERO_HASH, build_audit_event
from src.trust.canonical import sha256_json
from src.trust.ports import TrustConflictError
from src.trust.schema import MODELS, kind_of


def validate_relationships(kind, record, rows):
    """Mirror DB foreign keys/uniqueness in the deterministic test adapter."""
    tenant = record.tenant_id

    def require(target_kind, identifier, *, same_version=True):
        related = rows.get((target_kind, tenant, identifier))
        if related is None:
            raise TrustConflictError(f"missing {target_kind} in this tenant")
        related = related[0]
        if same_version:
            related_version = related.id if target_kind == "document_version" else related.document_version_id
            if related_version != record.document_version_id:
                raise TrustConflictError("relationship belongs to a different document version")
        return related

    peers = [v[0] for (k, t, _), v in rows.items() if k == kind and t == tenant]
    if kind == "document":
        if record.external_key and any(p.external_key == record.external_key for p in peers):
            raise TrustConflictError("external document key already exists")
    elif kind == "document_version":
        require("document", record.document_id, same_version=False)
        if any(p.content_sha256 == record.content_sha256 for p in peers):
            raise TrustConflictError("document bytes already registered")
        earlier = [p for p in peers if p.document_id == record.document_id]
        latest = max(earlier, key=lambda p: p.version_number) if earlier else None
        if latest is None:
            if record.version_number != 1 or record.parent_version_id is not None:
                raise TrustConflictError("version chain must start at 1")
        elif record.parent_version_id != latest.id or record.version_number != latest.version_number + 1:
            raise TrustConflictError("version must directly follow latest parent")
    elif kind == "reviewer_decision":
        require("finding", record.finding_id, same_version=False)
        if record.supersedes_decision_id:
            parent = require("reviewer_decision", record.supersedes_decision_id, same_version=False)
            if parent.finding_id != record.finding_id:
                raise TrustConflictError("supersession must be for the same finding")
            if any(p.supersedes_decision_id == parent.id for p in peers):
                raise TrustConflictError("decision already superseded")
    else:
        require("document_version", record.document_version_id)
        for field, target in (("source_span_id", "source_span"), ("reference_id", "reference"),
                              ("model_run_id", "model_run"), ("candidate_match_id", "candidate_match")):
            identifier = getattr(record, field, None)
            if identifier is not None:
                require(target, identifier)
        if kind == "reference":
            if any(p.document_version_id == record.document_version_id and
                   (p.reference_key == record.reference_key or
                    (p.ordinal == record.ordinal and p.bibliographic_metadata.get("extraction_revision", "legacy") ==
                     record.bibliographic_metadata.get("extraction_revision", "legacy"))) for p in peers):
                raise TrustConflictError("reference key or ordinal already exists")
        elif kind == "candidate_match":
            identity = lambda p: (p.reference_id, p.model_run_id, p.provider, p.external_id)
            if any(identity(p) == identity(record) for p in peers):
                raise TrustConflictError("candidate already exists")
        elif kind == "evidence":
            require(record.subject_type, record.subject_id)


class InMemoryTrustRepository:
    def __init__(self):
        self._lock = RLock()
        self._rows = {}
        self._events = {}

    def append_batch(self, records, actor):
        if not records:
            return []
        tenant = records[0].tenant_id
        if any(r.tenant_id != tenant for r in records):
            raise TrustConflictError("a batch must belong to one tenant")
        with self._lock:
            # Work on detached containers so invalid later records cannot partially commit.
            rows = dict(self._rows)
            events = list(self._events.get(tenant, []))
            created = []
            for record in records:
                kind = kind_of(record)
                record = MODELS[kind][1].model_validate(record.model_dump(mode="json"))
                key = (kind, tenant, record.id)
                digest = sha256_json(record)
                if key in rows:
                    if rows[key][1] != digest or sha256_json(rows[key][0]) != digest:
                        raise TrustConflictError("immutable identifier reused for different content")
                    created.append(False)
                    continue
                validate_relationships(kind, record, rows)
                rows[key] = (record, digest)
                events.append(build_audit_event(
                    tenant_id=tenant, sequence_number=len(events) + 1,
                    previous_hash=events[-1].event_hash if events else ZERO_HASH,
                    actor=actor, action=f"{kind}.recorded", aggregate_type=kind,
                    aggregate_id=record.id, payload={"record_hash": digest, "record_version": 1},
                ))
                created.append(True)
            self._rows = rows
            self._events[tenant] = events
            return created

    def get_record(self, kind, tenant_id, record_id):
        if kind not in MODELS:
            raise ValueError("unknown record kind")
        with self._lock:
            item = self._rows.get((kind, tenant_id, record_id))
            return item[0] if item else None

    def get_document_version_by_id(self, tenant_id, version_id):
        return self.get_record("document_version", tenant_id, version_id)

    def get_document_version_by_hash(self, tenant_id, digest):
        with self._lock:
            for (kind, tenant, _), (version, _) in self._rows.items():
                if kind == "document_version" and tenant == tenant_id and version.content_sha256 == digest:
                    return self.get_record("document", tenant_id, version.document_id), version
            return None

    def get_latest_document_version_by_external_key(self, tenant_id, key):
        with self._lock:
            document = next((r for (k, t, _), (r, _) in self._rows.items()
                             if k == "document" and t == tenant_id and r.external_key == key), None)
            if document is None:
                return None
            versions = [r for (k, t, _), (r, _) in self._rows.items()
                        if k == "document_version" and t == tenant_id and r.document_id == document.id]
            if not versions:
                raise TrustConflictError("document has no version")
            return document, max(versions, key=lambda r: r.version_number)

    def audit_snapshot(self, tenant_id):
        with self._lock:
            return list(self._events.get(tenant_id, [])), {
                (kind, identifier): pair for (kind, tenant, identifier), pair in self._rows.items()
                if tenant == tenant_id
            }
