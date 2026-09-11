"""Repository operations use one tenant and atomically append records/events."""
from pathlib import Path
from typing import Protocol
from uuid import UUID

from src.trust.models import Actor, AuditEvent, Record, StoredObject


class TrustKernelError(RuntimeError):
    pass


class TrustConflictError(TrustKernelError):
    pass


class TrustIntegrityError(TrustKernelError):
    pass


class TrustConfigurationError(TrustKernelError):
    pass


class ObjectStore(Protocol):
    def put_file(self, path: Path, *, content_sha256: str, media_type: str) -> StoredObject: ...


class TrustRepository(Protocol):
    def append_batch(self, records: list[Record], actor: Actor) -> list[bool]: ...
    def get_record(self, kind: str, tenant_id: UUID, record_id: UUID) -> Record | None: ...
    def get_document_version_by_hash(self, tenant_id: UUID, digest: str) -> tuple | None: ...
    def get_latest_document_version_by_external_key(self, tenant_id: UUID, key: str) -> tuple | None: ...
    def audit_snapshot(self, tenant_id: UUID) -> tuple[list[AuditEvent], dict]: ...
