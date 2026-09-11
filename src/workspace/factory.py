"""Construct the workspace runtime from configuration.

One place decides which store, object backend, signer and queue this process
uses, so the API, the worker and the scripts cannot drift apart.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def build_workspace_store(config):
    """SQLite for one workstation, PostgreSQL for the durable runtime."""
    backend = getattr(config, "WORKSPACE_BACKEND", "sqlite")
    if backend == "postgres":
        from src.workspace.postgres_store import PostgresWorkspaceStore

        return PostgresWorkspaceStore(
            config.WORKSPACE_DATABASE_URL,
            schema=config.WORKSPACE_DATABASE_SCHEMA,
            min_size=config.WORKSPACE_POOL_MIN,
            max_size=config.WORKSPACE_POOL_MAX,
        )
    from src.workspace.store import WorkspaceStore

    return WorkspaceStore(config.WORKSPACE_DATABASE_PATH)


def build_signer(config):
    from src.workspace.signing import Signer

    return Signer.from_config(config)


def build_pricing(config):
    from src.telemetry.pricing import PricingTable

    return PricingTable.load(getattr(config, "PRICING_TABLE_PATH", None))


def build_runtime(config, *, with_pipeline: bool = True):
    """Assemble every long-lived component this process needs.

    Returns a dictionary rather than a class so a caller can take only the parts
    it uses — the worker needs no export service, the API needs no pipeline when
    a separate worker is running.
    """
    from config.settings import Config as _Config  # noqa: F401  (import side effects)
    from src.workspace.objects import build_object_store
    from src.workspace.service import WorkspaceService

    config.ensure_directories()
    store = build_workspace_store(config)
    object_store = build_object_store(config)

    legacy = None
    pipeline = None
    if with_pipeline:
        from database_manager import DatabaseManager
        from pipeline_orchestrator import PipelineOrchestrator
        from src.trust.factory import build_trust_kernel

        legacy = DatabaseManager(config.DATABASE_PATH)
        pipeline = PipelineOrchestrator(
            legacy, config, trust_kernel=build_trust_kernel(config)
        )

    service = WorkspaceService(
        store,
        data_root=config.DATA_DIR,
        max_pdf_bytes=config.MAX_PDF_BYTES,
        legacy_db=legacy,
    )
    # Give the service the Phase 4 collaborators when it knows how to use them.
    for attribute, value in (
        ("object_store", object_store),
        ("config", config),
    ):
        try:
            setattr(service, attribute, value)
        except AttributeError:  # pragma: no cover - defensive
            logger.debug("WorkspaceService does not accept %s", attribute)

    return {
        "config": config,
        "store": store,
        "object_store": object_store,
        "service": service,
        "pipeline": pipeline,
        "legacy_db": legacy,
        "signer": build_signer(config),
        "pricing": build_pricing(config),
    }


def build_export_service(runtime):
    from src.workspace.exports import ExportService

    return ExportService(
        runtime["service"],
        runtime["store"],
        runtime["object_store"],
        signer=runtime["signer"],
        retention_days=getattr(runtime["config"], "EXPORT_RETENTION_DAYS", 365),
    )


def build_repair_service(runtime, runner=None):
    from src.workspace.repair import RepairService

    return RepairService(runtime["service"], runtime["store"], runner=runner)


def ensure_workspace_schema(config) -> list[str]:
    """Apply PostgreSQL workspace migrations when that backend is selected."""
    if getattr(config, "WORKSPACE_BACKEND", "sqlite") != "postgres":
        return []
    from src.workspace.migrations import apply_workspace_migrations

    dsn = config.WORKSPACE_MIGRATION_DATABASE_URL or config.WORKSPACE_DATABASE_URL
    return apply_workspace_migrations(dsn, schema=config.WORKSPACE_DATABASE_SCHEMA)


def resolve_data_path(config, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path(config.BASE_DIR) / path


__all__ = [
    "build_export_service",
    "build_pricing",
    "build_repair_service",
    "build_runtime",
    "build_signer",
    "build_workspace_store",
    "ensure_workspace_schema",
    "resolve_data_path",
]
