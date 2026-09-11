"""Construct the optional kernel without importing database/S3 drivers when disabled."""
from pathlib import Path
from uuid import UUID

from src.trust.models import Actor
from src.trust.object_store import LocalObjectStore, S3ObjectStore
from src.trust.postgres_repository import PostgresTrustRepository
from src.trust.service import TrustKernel


def build_trust_kernel(config):
    if not config.TRUST_KERNEL_ENABLED:
        return None
    config.validate_trust_kernel()
    tenant = UUID(config.TRUST_TENANT_ID)
    repository = PostgresTrustRepository(config.TRUST_DATABASE_URL,
                                         expected_role=config.TRUST_DATABASE_RUNTIME_ROLE)
    if config.TRUST_OBJECT_STORE_BACKEND == "local":
        root = Path(config.TRUST_OBJECT_STORE_DIR)
        if not root.is_absolute():
            root = config.BASE_DIR / root
        store = LocalObjectStore(root, namespace=str(tenant))
    else:
        prefix = config.TRUST_S3_PREFIX.strip("/")
        store = S3ObjectStore(
            bucket=config.TRUST_S3_BUCKET, prefix=f"{prefix}/{tenant}" if prefix else str(tenant),
            endpoint_url=config.TRUST_S3_ENDPOINT_URL or None, region_name=config.TRUST_S3_REGION or None,
            server_side_encryption=config.TRUST_S3_SERVER_SIDE_ENCRYPTION,
            kms_key_id=config.TRUST_S3_KMS_KEY_ID or None,
            require_versioning=config.TRUST_S3_REQUIRE_VERSIONING,
        )
    return TrustKernel(repository, store, tenant_id=tenant,
                       actor=Actor(actor_type="service", actor_id=config.TRUST_ACTOR_ID),
                       max_pdf_bytes=config.MAX_PDF_BYTES)
