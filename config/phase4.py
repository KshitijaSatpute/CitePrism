"""Phase 4 enterprise runtime configuration.

Every value is read once at import time so that the settings object stays a
plain class, matching the existing Phase 0-3 style. Validation is explicit and
is only executed by entry points, never by an import.
"""

from __future__ import annotations

import os
from typing import Optional
from uuid import UUID

from config.trust import env_bool


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError as exc:  # pragma: no cover - configuration error path
        raise ValueError(f"{name} must be an integer") from exc


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except ValueError as exc:  # pragma: no cover - configuration error path
        raise ValueError(f"{name} must be a number") from exc


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.getenv(name, default) or ""
    return tuple(item.strip() for item in raw.split(",") if item.strip())


class Phase4Settings:
    """Durable runtime, evidence acquisition, identity and telemetry settings."""

    # ------------------------------------------------------------------
    # Workspace persistence
    # ------------------------------------------------------------------
    # "sqlite" keeps the Phase 3 single-workstation behavior. "postgres"
    # activates the durable multi-process runtime.
    WORKSPACE_BACKEND: str = os.getenv("CITEPRISM_WORKSPACE_BACKEND", "sqlite").strip().lower()
    WORKSPACE_DATABASE_URL: Optional[str] = os.getenv("CITEPRISM_WORKSPACE_DATABASE_URL")
    WORKSPACE_MIGRATION_DATABASE_URL: Optional[str] = os.getenv(
        "CITEPRISM_WORKSPACE_MIGRATION_DATABASE_URL"
    )
    WORKSPACE_DATABASE_SCHEMA: str = os.getenv("CITEPRISM_WORKSPACE_SCHEMA", "citeprism_workspace")
    WORKSPACE_POOL_MIN: int = _int("CITEPRISM_WORKSPACE_POOL_MIN", 1)
    WORKSPACE_POOL_MAX: int = _int("CITEPRISM_WORKSPACE_POOL_MAX", 8)

    # ------------------------------------------------------------------
    # Durable queue
    # ------------------------------------------------------------------
    QUEUE_LEASE_SECONDS: int = _int("CITEPRISM_QUEUE_LEASE_SECONDS", 120)
    QUEUE_HEARTBEAT_SECONDS: int = _int("CITEPRISM_QUEUE_HEARTBEAT_SECONDS", 20)
    QUEUE_POLL_SECONDS: float = _float("CITEPRISM_QUEUE_POLL_SECONDS", 2.0)
    QUEUE_MAX_ATTEMPTS: int = _int("CITEPRISM_QUEUE_MAX_ATTEMPTS", 3)
    QUEUE_BACKOFF_SECONDS: float = _float("CITEPRISM_QUEUE_BACKOFF_SECONDS", 15.0)
    QUEUE_BACKOFF_CEILING_SECONDS: float = _float("CITEPRISM_QUEUE_BACKOFF_CEILING", 900.0)
    # An in-process runner remains available for the local developer profile.
    RUN_EMBEDDED_WORKER: bool = env_bool("CITEPRISM_RUN_EMBEDDED_WORKER", True)

    # ------------------------------------------------------------------
    # Workspace object storage (S3 compatible: AWS S3, MinIO, Ceph RGW)
    # ------------------------------------------------------------------
    WORKSPACE_OBJECT_BACKEND: str = os.getenv("CITEPRISM_OBJECT_BACKEND", "local").strip().lower()
    WORKSPACE_OBJECT_DIR: str = os.getenv("CITEPRISM_OBJECT_DIR", "data/workspace_objects")
    WORKSPACE_S3_BUCKET: Optional[str] = os.getenv("CITEPRISM_S3_BUCKET")
    WORKSPACE_S3_PREFIX: str = os.getenv("CITEPRISM_S3_PREFIX", "citeprism/workspace")
    WORKSPACE_S3_ENDPOINT_URL: Optional[str] = os.getenv("CITEPRISM_S3_ENDPOINT_URL")
    WORKSPACE_S3_REGION: Optional[str] = os.getenv("CITEPRISM_S3_REGION")
    WORKSPACE_S3_ADDRESSING: str = os.getenv("CITEPRISM_S3_ADDRESSING", "path").strip().lower()
    WORKSPACE_S3_SERVER_SIDE_ENCRYPTION: Optional[str] = os.getenv(
        "CITEPRISM_S3_SERVER_SIDE_ENCRYPTION", "AES256"
    )
    WORKSPACE_S3_KMS_KEY_ID: Optional[str] = os.getenv("CITEPRISM_S3_KMS_KEY_ID")
    WORKSPACE_S3_REQUIRE_VERSIONING: bool = env_bool("CITEPRISM_S3_REQUIRE_VERSIONING", False)
    WORKSPACE_S3_SIGNED_URL_SECONDS: int = _int("CITEPRISM_S3_SIGNED_URL_SECONDS", 300)

    # ------------------------------------------------------------------
    # Identity: OIDC / RBAC / organizations
    # ------------------------------------------------------------------
    # local | trusted_proxy | oidc
    OIDC_ISSUER: Optional[str] = os.getenv("CITEPRISM_OIDC_ISSUER")
    OIDC_AUDIENCE: Optional[str] = os.getenv("CITEPRISM_OIDC_AUDIENCE")
    OIDC_JWKS_URL: Optional[str] = os.getenv("CITEPRISM_OIDC_JWKS_URL")
    OIDC_ALGORITHMS: tuple[str, ...] = _csv("CITEPRISM_OIDC_ALGORITHMS", "RS256,ES256")
    OIDC_TENANT_CLAIM: str = os.getenv("CITEPRISM_OIDC_TENANT_CLAIM", "org_id")
    OIDC_ROLES_CLAIM: str = os.getenv("CITEPRISM_OIDC_ROLES_CLAIM", "roles")
    OIDC_SUBJECT_CLAIM: str = os.getenv("CITEPRISM_OIDC_SUBJECT_CLAIM", "sub")
    OIDC_EMAIL_CLAIM: str = os.getenv("CITEPRISM_OIDC_EMAIL_CLAIM", "email")
    OIDC_NAME_CLAIM: str = os.getenv("CITEPRISM_OIDC_NAME_CLAIM", "name")
    OIDC_JWKS_CACHE_SECONDS: int = _int("CITEPRISM_OIDC_JWKS_CACHE_SECONDS", 600)
    OIDC_CLOCK_SKEW_SECONDS: int = _int("CITEPRISM_OIDC_CLOCK_SKEW_SECONDS", 60)
    OIDC_REQUIRE_TENANT_CLAIM: bool = env_bool("CITEPRISM_OIDC_REQUIRE_TENANT_CLAIM", True)
    # Directory role name -> CitePrism role. Format: "dir-role=citeprism-role,..."
    OIDC_ROLE_MAP: str = os.getenv("CITEPRISM_OIDC_ROLE_MAP", "")

    # ------------------------------------------------------------------
    # Evidence acquisition
    # ------------------------------------------------------------------
    ENABLE_OCR: bool = env_bool("CITEPRISM_ENABLE_OCR", False)
    OCR_ENGINE: str = os.getenv("CITEPRISM_OCR_ENGINE", "tesseract").strip().lower()
    OCR_LANGUAGES: str = os.getenv("CITEPRISM_OCR_LANGUAGES", "eng")
    OCR_DPI: int = _int("CITEPRISM_OCR_DPI", 300)
    OCR_MAX_PAGES: int = _int("CITEPRISM_OCR_MAX_PAGES", 60)
    # A page below this many extracted characters is treated as image-only.
    OCR_MIN_CHARS_PER_PAGE: int = _int("CITEPRISM_OCR_MIN_CHARS_PER_PAGE", 120)
    OCR_MIN_TEXT_PAGE_RATIO: float = _float("CITEPRISM_OCR_MIN_TEXT_PAGE_RATIO", 0.6)

    ENABLE_FULL_TEXT_ACQUISITION: bool = env_bool("CITEPRISM_ENABLE_FULL_TEXT", False)
    FULL_TEXT_MAX_BYTES: int = _int("CITEPRISM_FULL_TEXT_MAX_BYTES", 25 * 1024 * 1024)
    FULL_TEXT_MAX_CHARS: int = _int("CITEPRISM_FULL_TEXT_MAX_CHARS", 400_000)
    UNPAYWALL_EMAIL: Optional[str] = os.getenv("UNPAYWALL_EMAIL")
    ENABLE_UNPAYWALL: bool = env_bool("CITEPRISM_ENABLE_UNPAYWALL", False)

    # ------------------------------------------------------------------
    # Identity resolution providers
    # ------------------------------------------------------------------
    RESOLUTION_PROVIDERS: tuple[str, ...] = _csv(
        "CITEPRISM_RESOLUTION_PROVIDERS", "openalex,crossref,semantic_scholar"
    )
    SEMANTIC_SCHOLAR_API_KEY: Optional[str] = os.getenv("SEMANTIC_SCHOLAR_API_KEY")
    SEMANTIC_SCHOLAR_BASE_URL: str = os.getenv(
        "SEMANTIC_SCHOLAR_BASE_URL", "https://api.semanticscholar.org/graph/v1"
    )
    CROSSREF_BASE_URL: str = os.getenv("CROSSREF_BASE_URL", "https://api.crossref.org")
    CROSSREF_MAILTO: Optional[str] = os.getenv("CROSSREF_MAILTO")
    RESOLUTION_CANDIDATES_PER_PROVIDER: int = _int("CITEPRISM_RESOLUTION_CANDIDATES", 8)
    RESOLUTION_TIMEOUT_SECONDS: float = _float("CITEPRISM_RESOLUTION_TIMEOUT", 35.0)

    # ------------------------------------------------------------------
    # Review package export signing
    # ------------------------------------------------------------------
    # ed25519 (asymmetric, verifiable by third parties) or hmac-sha256.
    EXPORT_SIGNING_ALGORITHM: str = os.getenv("CITEPRISM_EXPORT_SIGNING", "ed25519").strip().lower()
    EXPORT_SIGNING_KEY_PATH: Optional[str] = os.getenv("CITEPRISM_EXPORT_SIGNING_KEY")
    EXPORT_SIGNING_KEY_ID: str = os.getenv("CITEPRISM_EXPORT_SIGNING_KEY_ID", "citeprism-export-1")
    EXPORT_HMAC_SECRET: Optional[str] = os.getenv("CITEPRISM_EXPORT_HMAC_SECRET")
    EXPORT_RETENTION_DAYS: int = _int("CITEPRISM_EXPORT_RETENTION_DAYS", 365)

    # ------------------------------------------------------------------
    # Telemetry, cost and SLOs
    # ------------------------------------------------------------------
    ENABLE_TELEMETRY: bool = env_bool("CITEPRISM_ENABLE_TELEMETRY", True)
    TELEMETRY_RETENTION_DAYS: int = _int("CITEPRISM_TELEMETRY_RETENTION_DAYS", 90)
    PRICING_TABLE_PATH: Optional[str] = os.getenv("CITEPRISM_PRICING_TABLE")
    SLO_AUDIT_P95_SECONDS: float = _float("CITEPRISM_SLO_AUDIT_P95_SECONDS", 900.0)
    SLO_QUEUE_WAIT_P95_SECONDS: float = _float("CITEPRISM_SLO_QUEUE_WAIT_P95_SECONDS", 60.0)
    SLO_AUDIT_SUCCESS_RATIO: float = _float("CITEPRISM_SLO_AUDIT_SUCCESS_RATIO", 0.98)
    SLO_RESOLUTION_RATIO: float = _float("CITEPRISM_SLO_RESOLUTION_RATIO", 0.85)
    SLO_SYSTEM_ISSUE_RATIO: float = _float("CITEPRISM_SLO_SYSTEM_ISSUE_RATIO", 0.05)
    SLO_COST_PER_AUDIT_USD: float = _float("CITEPRISM_SLO_COST_PER_AUDIT_USD", 1.50)
    SLO_WINDOW_HOURS: int = _int("CITEPRISM_SLO_WINDOW_HOURS", 168)

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------
    EVALUATION_DATASET_DIR: str = os.getenv("CITEPRISM_EVAL_DATASET_DIR", "data/evaluation")
    EVALUATION_CALIBRATION_BINS: int = _int("CITEPRISM_EVAL_CALIBRATION_BINS", 10)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @classmethod
    def phase4_errors(cls) -> list[str]:
        errors: list[str] = []

        if cls.WORKSPACE_BACKEND not in {"sqlite", "postgres"}:
            errors.append("CITEPRISM_WORKSPACE_BACKEND must be sqlite or postgres")
        if cls.WORKSPACE_BACKEND == "postgres":
            dsn = cls.WORKSPACE_DATABASE_URL or ""
            if not dsn.startswith(("postgresql://", "postgres://")):
                errors.append(
                    "CITEPRISM_WORKSPACE_DATABASE_URL must be a PostgreSQL DSN "
                    "when the workspace backend is postgres"
                )
            if not cls.WORKSPACE_DATABASE_SCHEMA.replace("_", "").isalnum():
                errors.append("CITEPRISM_WORKSPACE_SCHEMA must be alphanumeric or underscore")
        if not 1 <= cls.WORKSPACE_POOL_MIN <= cls.WORKSPACE_POOL_MAX <= 64:
            errors.append("workspace pool sizes must satisfy 1 <= min <= max <= 64")

        if cls.QUEUE_HEARTBEAT_SECONDS >= cls.QUEUE_LEASE_SECONDS:
            errors.append("CITEPRISM_QUEUE_HEARTBEAT_SECONDS must be shorter than the lease")
        if not 1 <= cls.QUEUE_MAX_ATTEMPTS <= 10:
            errors.append("CITEPRISM_QUEUE_MAX_ATTEMPTS must be between 1 and 10")

        if cls.WORKSPACE_OBJECT_BACKEND not in {"local", "s3"}:
            errors.append("CITEPRISM_OBJECT_BACKEND must be local or s3")
        if cls.WORKSPACE_OBJECT_BACKEND == "s3":
            if not cls.WORKSPACE_S3_BUCKET:
                errors.append("CITEPRISM_S3_BUCKET is required for the s3 object backend")
            if cls.WORKSPACE_S3_ADDRESSING not in {"path", "virtual"}:
                errors.append("CITEPRISM_S3_ADDRESSING must be path or virtual")
            if cls.WORKSPACE_S3_KMS_KEY_ID and cls.WORKSPACE_S3_SERVER_SIDE_ENCRYPTION == "AES256":
                errors.append("a KMS key requires CITEPRISM_S3_SERVER_SIDE_ENCRYPTION=aws:kms")

        # WORKSPACE_AUTH_MODE is contributed by the sibling settings class.
        if getattr(cls, "WORKSPACE_AUTH_MODE", "local") == "oidc":
            if not cls.OIDC_ISSUER:
                errors.append("CITEPRISM_OIDC_ISSUER is required in oidc mode")
            if not cls.OIDC_AUDIENCE:
                errors.append("CITEPRISM_OIDC_AUDIENCE is required in oidc mode")
            if not cls.OIDC_ALGORITHMS:
                errors.append("CITEPRISM_OIDC_ALGORITHMS must list at least one algorithm")
            unsupported = set(cls.OIDC_ALGORITHMS) - {"RS256", "RS384", "RS512", "ES256", "ES384"}
            if unsupported:
                errors.append(f"unsupported OIDC algorithms: {', '.join(sorted(unsupported))}")

        if cls.ENABLE_OCR and cls.OCR_ENGINE not in {"tesseract", "none"}:
            errors.append("CITEPRISM_OCR_ENGINE must be tesseract or none")
        if not 72 <= cls.OCR_DPI <= 600:
            errors.append("CITEPRISM_OCR_DPI must be between 72 and 600")

        if cls.ENABLE_UNPAYWALL and not cls.UNPAYWALL_EMAIL:
            errors.append("UNPAYWALL_EMAIL is required when Unpaywall lookups are enabled")

        unknown_providers = set(cls.RESOLUTION_PROVIDERS) - {
            "openalex",
            "crossref",
            "semantic_scholar",
        }
        if unknown_providers:
            errors.append(f"unknown resolution providers: {', '.join(sorted(unknown_providers))}")
        if not cls.RESOLUTION_PROVIDERS:
            errors.append("at least one resolution provider is required")

        if cls.EXPORT_SIGNING_ALGORITHM not in {"ed25519", "hmac-sha256", "none"}:
            errors.append("CITEPRISM_EXPORT_SIGNING must be ed25519, hmac-sha256 or none")
        if cls.EXPORT_SIGNING_ALGORITHM == "hmac-sha256" and not cls.EXPORT_HMAC_SECRET:
            errors.append("CITEPRISM_EXPORT_HMAC_SECRET is required for hmac-sha256 signing")

        for name, value in (
            ("CITEPRISM_SLO_AUDIT_SUCCESS_RATIO", cls.SLO_AUDIT_SUCCESS_RATIO),
            ("CITEPRISM_SLO_RESOLUTION_RATIO", cls.SLO_RESOLUTION_RATIO),
            ("CITEPRISM_SLO_SYSTEM_ISSUE_RATIO", cls.SLO_SYSTEM_ISSUE_RATIO),
        ):
            if not 0.0 <= value <= 1.0:
                errors.append(f"{name} must be between 0 and 1")

        if cls.EVALUATION_CALIBRATION_BINS < 2 or cls.EVALUATION_CALIBRATION_BINS > 100:
            errors.append("CITEPRISM_EVAL_CALIBRATION_BINS must be between 2 and 100")

        return errors

    @classmethod
    def validate_phase4(cls) -> None:
        errors = cls.phase4_errors()
        if errors:
            raise ValueError(
                "Phase 4 configuration errors:\n" + "\n".join(f"  - {item}" for item in errors)
            )

    @classmethod
    def oidc_role_map(cls) -> dict[str, str]:
        """Directory group/role name to CitePrism role name."""
        mapping: dict[str, str] = {}
        for pair in cls.OIDC_ROLE_MAP.split(","):
            if "=" not in pair:
                continue
            source, target = pair.split("=", 1)
            source, target = source.strip(), target.strip().lower()
            if source and target:
                mapping[source] = target
        return mapping

    @classmethod
    def default_organization_id(cls) -> str:
        value = os.getenv(
            "CITEPRISM_DEFAULT_TENANT_ID", "00000000-0000-4000-8000-000000000001"
        )
        return str(UUID(value))
