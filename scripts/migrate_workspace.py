#!/usr/bin/env python
"""Apply the PostgreSQL workspace migrations.

Migrations run under an administrator DSN. The runtime role passed with
--runtime-role receives least-privilege grants: it can update operational
tables, but it can only INSERT and SELECT on the append-only activity,
decision and telemetry tables.

    python scripts/migrate_workspace.py \
        --database-url postgresql://admin:...@localhost:5433/citeprism \
        --runtime-role citeprism_workspace_app
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply CitePrism workspace migrations")
    parser.add_argument("--database-url", default=None, help="administrator PostgreSQL DSN")
    parser.add_argument("--schema", default=None, help="workspace schema name")
    parser.add_argument("--runtime-role", default=None, help="unprivileged runtime login role")
    arguments = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from config.settings import Config
    from src.workspace.migrations import apply_workspace_migrations

    dsn = (
        arguments.database_url
        or Config.WORKSPACE_MIGRATION_DATABASE_URL
        or Config.WORKSPACE_DATABASE_URL
    )
    if not dsn:
        parser.error(
            "supply --database-url or set CITEPRISM_WORKSPACE_MIGRATION_DATABASE_URL"
        )
    applied = apply_workspace_migrations(
        dsn,
        schema=arguments.schema or Config.WORKSPACE_DATABASE_SCHEMA,
        runtime_role=arguments.runtime_role,
    )
    if applied:
        print("Applied: " + ", ".join(applied))
    else:
        print("The workspace schema is already current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
