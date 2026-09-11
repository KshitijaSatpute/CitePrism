"""Optional trust configuration, loaded after the existing dotenv setup."""
import os
from uuid import UUID


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip().lower()
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true/false, 1/0, yes/no, or on/off")


class TrustSettings:
    TRUST_KERNEL_ENABLED = env_bool("TRUST_KERNEL_ENABLED")
    TRUST_DATABASE_URL = os.getenv("TRUST_DATABASE_URL")
    TRUST_MIGRATION_DATABASE_URL = os.getenv("TRUST_MIGRATION_DATABASE_URL")
    TRUST_DATABASE_RUNTIME_ROLE = os.getenv("TRUST_DATABASE_RUNTIME_ROLE")
    TRUST_TENANT_ID = os.getenv("TRUST_TENANT_ID")
    TRUST_ACTOR_ID = os.getenv("TRUST_ACTOR_ID", "citeprism-streamlit")
    TRUST_OBJECT_STORE_BACKEND = os.getenv("TRUST_OBJECT_STORE_BACKEND", "local")
    TRUST_OBJECT_STORE_DIR = os.getenv("TRUST_OBJECT_STORE_DIR", "data/trust_objects")
    TRUST_S3_BUCKET = os.getenv("TRUST_S3_BUCKET")
    TRUST_S3_PREFIX = os.getenv("TRUST_S3_PREFIX", "citeprism")
    TRUST_S3_ENDPOINT_URL = os.getenv("TRUST_S3_ENDPOINT_URL")
    TRUST_S3_REGION = os.getenv("TRUST_S3_REGION")
    TRUST_S3_SERVER_SIDE_ENCRYPTION = os.getenv("TRUST_S3_SERVER_SIDE_ENCRYPTION", "AES256")
    TRUST_S3_KMS_KEY_ID = os.getenv("TRUST_S3_KMS_KEY_ID")
    TRUST_S3_REQUIRE_VERSIONING = env_bool("TRUST_S3_REQUIRE_VERSIONING", True)

    @classmethod
    def validate_trust_kernel(cls):
        if not cls.TRUST_DATABASE_URL or not cls.TRUST_DATABASE_URL.startswith(("postgresql://", "postgres://")):
            raise ValueError("TRUST_DATABASE_URL must be a PostgreSQL runtime DSN")
        if not cls.TRUST_DATABASE_RUNTIME_ROLE or not cls.TRUST_DATABASE_RUNTIME_ROLE.strip():
            raise ValueError("TRUST_DATABASE_RUNTIME_ROLE is required")
        try:
            UUID(str(cls.TRUST_TENANT_ID))
        except ValueError as exc:
            raise ValueError("TRUST_TENANT_ID must be a UUID") from exc
        if not cls.TRUST_ACTOR_ID or not cls.TRUST_ACTOR_ID.strip():
            raise ValueError("TRUST_ACTOR_ID cannot be empty")
        if cls.TRUST_OBJECT_STORE_BACKEND not in {"local", "s3"}:
            raise ValueError("TRUST_OBJECT_STORE_BACKEND must be local or s3")
        if cls.TRUST_OBJECT_STORE_BACKEND == "s3" and not cls.TRUST_S3_BUCKET:
            raise ValueError("TRUST_S3_BUCKET is required")
