"""Operator migration, ingestion, capture, and verification commands."""
import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

from config.settings import Config
from src.trust.factory import build_trust_kernel
from src.trust.migrations import apply_migrations


def main(argv=None):
    parser = argparse.ArgumentParser(prog="citeprism-trust")
    commands = parser.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser("migrate")
    migrate.add_argument("--database-url", default=Config.TRUST_MIGRATION_DATABASE_URL)
    migrate.add_argument("--runtime-role", default=Config.TRUST_DATABASE_RUNTIME_ROLE)
    ingest = commands.add_parser("ingest")
    ingest.add_argument("pdf", type=Path)
    ingest.add_argument("--original-filename")
    ingest.add_argument("--title")
    ingest.add_argument("--external-key")
    capture = commands.add_parser("capture")
    capture.add_argument("document_version_id", type=UUID)
    capture.add_argument("scored_json", type=Path)
    commands.add_parser("verify-audit")
    args = parser.parse_args(argv)
    try:
        if args.command == "migrate":
            print(json.dumps({"applied": apply_migrations(args.database_url, runtime_role=args.runtime_role)}))
            return 0
        kernel = build_trust_kernel(Config)
        if kernel is None:
            raise ValueError("set TRUST_KERNEL_ENABLED=true first")
        if args.command == "ingest":
            result = kernel.ingest_pdf(args.pdf, original_filename=args.original_filename,
                                       title=args.title, external_key=args.external_key)
        elif args.command == "capture":
            result = kernel.capture_legacy_scored_artifact(args.document_version_id, args.scored_json)
        else:
            result = kernel.verify_audit_chain()
        print(json.dumps(result.model_dump(mode="json"), indent=2))
        return 2 if args.command == "verify-audit" and not result.valid else 0
    except Exception as exc:
        # Avoid printing database connection strings or upstream provider bodies.
        from src.trust.ports import TrustKernelError
        message = str(exc) if isinstance(exc, (ValueError, TrustKernelError)) else type(exc).__name__
        print(f"error: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
