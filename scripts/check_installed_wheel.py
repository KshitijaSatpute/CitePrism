"""Run with the wheel installed and from outside the source checkout."""
from pathlib import Path
from src.trust.cli import main
from src.trust.migrations import default_migrations_dir
from src.trust.models import DocumentRecord

source = default_migrations_dir() / "001_trust_kernel.sql"
assert source.is_file(), "wheel omitted migration SQL"
assert "FORCE ROW LEVEL SECURITY" in source.read_text(encoding="utf-8")
assert (default_migrations_dir() / "002_extraction_observations.sql").is_file(), "wheel omitted Phase 2 migration"
assert "record_version" in DocumentRecord.model_fields
print("Installed wheel includes CLI, configuration, contracts, and SQL migrations.")
