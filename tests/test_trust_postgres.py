"""Live PostgreSQL 16+ contract tests. Run only against a disposable dev database.

Absent DSNs skip visibly. Supplied but broken DSNs/drivers FAIL, never skip.
No test drops schemas, truncates tables, or deletes user data. Fresh UUID tenants
leave small test records in the disposable database.
"""
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from src.trust.migrations import apply_migrations, default_migrations_dir
from src.trust.models import DocumentRecord, Evidence, ReferenceRecord
from src.trust.ports import TrustConfigurationError, TrustConflictError, TrustIntegrityError
from src.trust.postgres_repository import PostgresTrustRepository
from tests.trust_fixtures import make_kernel, make_pdf, make_scored

ADMIN_DSN = os.getenv("CITEPRISM_TEST_ADMIN_DATABASE_URL")
RUNTIME_DSN = os.getenv("CITEPRISM_TEST_DATABASE_URL")
RUNTIME_ROLE = os.getenv("CITEPRISM_TEST_RUNTIME_ROLE", "citeprism_app")


@unittest.skipUnless(ADMIN_DSN and RUNTIME_DSN, "PostgreSQL integration requires dedicated test admin/runtime DSNs")
class PostgresTrustTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.psycopg = psycopg
        apply_migrations(ADMIN_DSN, runtime_role=RUNTIME_ROLE)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = PostgresTrustRepository(RUNTIME_DSN, expected_role=RUNTIME_ROLE)
        self.kernel = make_kernel(self.root, self.repo)
        self.pdf = make_pdf(self.root)
        self.receipt = self.kernel.ingest_pdf(self.pdf)

    def test_roundtrip_capture_retry_and_review(self):
        first = self.kernel.capture_legacy_scored_artifact(self.receipt.document_version_id, make_scored(self.root))
        second = self.kernel.capture_legacy_scored_artifact(self.receipt.document_version_id, make_scored(self.root))
        self.assertEqual(first.model_run_id, second.model_run_id)
        self.assertEqual(second.references_created + second.evidence_created + second.findings_created, 0)
        events, records = self.repo.audit_snapshot(self.kernel.tenant_id)
        finding = next(r for (kind, _), (r, _) in records.items() if kind == "finding")
        self.kernel.record_reviewer_decision(finding_id=finding.id, disposition="needs_more_evidence",
                                             rationale="Await source", reviewer_id="test-reviewer")
        result = self.kernel.verify_audit_chain()
        self.assertTrue(result.valid, result.error)
        self.assertEqual(result.event_count, len(events) + 1)

    def test_no_tenant_and_other_tenant_raw_reads_are_empty(self):
        with self.psycopg.connect(RUNTIME_DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT id FROM citeprism.documents WHERE id = %s", (self.receipt.document_id,))
                self.assertEqual(cursor.fetchall(), [])
                cursor.execute("SELECT set_config('app.tenant_id', %s, true)", (str(uuid4()),))
                cursor.execute("SELECT id FROM citeprism.documents WHERE id = %s", (self.receipt.document_id,))
                self.assertEqual(cursor.fetchall(), [])

    def test_runtime_cannot_mutate_and_admin_cannot_accidentally_mutate(self):
        for dsn in (RUNTIME_DSN, ADMIN_DSN):
            for command in ("UPDATE citeprism.documents SET title = title WHERE false",
                            "DELETE FROM citeprism.documents WHERE false"):
                with self.subTest(dsn="runtime" if dsn == RUNTIME_DSN else "admin", command=command):
                    with self.assertRaises(self.psycopg.Error), self.psycopg.connect(dsn) as connection:
                        connection.execute(command)
        # Verify TRUNCATE is denied without executing any destructive operation.
        with self.psycopg.connect(RUNTIME_DSN) as connection:
            self.assertFalse(connection.execute("SELECT has_table_privilege(current_user, 'citeprism.documents', 'TRUNCATE')").fetchone()[0])

    def test_administrator_is_rejected_as_runtime(self):
        with self.assertRaises(TrustConfigurationError):
            PostgresTrustRepository(ADMIN_DSN).audit_snapshot(self.kernel.tenant_id)

    def test_batch_rollback_and_cross_document_subject_constraint(self):
        before = self.repo.audit_snapshot(self.kernel.tenant_id)
        doc = DocumentRecord(id=uuid4(), tenant_id=self.kernel.tenant_id,
                             created_at=before[0][0].occurred_at, created_by="test")
        bad = ReferenceRecord(id=uuid4(), tenant_id=self.kernel.tenant_id, document_version_id=uuid4(),
                              reference_key="bad", ordinal=1, raw_text_sha256="a" * 64,
                              created_at=before[0][0].occurred_at)
        with self.assertRaises(TrustConflictError):
            self.repo.append_batch([doc, bad], self.kernel.actor)
        self.assertEqual(self.repo.audit_snapshot(self.kernel.tenant_id), before)
        other = self.kernel.ingest_pdf(make_pdf(self.root, "other.pdf", b"other"))
        bad_evidence = Evidence(id=uuid4(), tenant_id=self.kernel.tenant_id,
                                document_version_id=other.document_version_id, subject_type="document_version",
                                subject_id=self.receipt.document_version_id, kind="policy_check",
                                statement="wrong document", created_at=doc.created_at)
        with self.assertRaises(TrustConflictError):
            self.repo.append_batch([bad_evidence], self.kernel.actor)

    def test_concurrent_ingest_single_version_and_valid_chain(self):
        path = make_pdf(self.root, "concurrent.pdf", b"concurrent")
        with ThreadPoolExecutor(max_workers=4) as executor:
            receipts = list(executor.map(lambda _: self.kernel.ingest_pdf(path), range(8)))
        self.assertEqual(len({r.document_version_id for r in receipts}), 1)
        self.assertTrue(self.kernel.verify_audit_chain().valid)

    def test_migration_repeat_and_checksum_drift(self):
        self.assertEqual(apply_migrations(ADMIN_DSN, runtime_role=RUNTIME_ROLE), [])
        directory = self.root / "migrations"
        directory.mkdir()
        source = default_migrations_dir() / "001_trust_kernel.sql"
        (directory / source.name).write_text(source.read_text(encoding="utf-8") + "\n-- drift\n", encoding="utf-8")
        with self.assertRaises(TrustIntegrityError):
            apply_migrations(ADMIN_DSN, directory, runtime_role=RUNTIME_ROLE)
        self.assertTrue(self.kernel.verify_audit_chain().valid)

    def test_skipped_revision_and_runtime_schema_creation_denied(self):
        version = self.repo.get_record("document_version", self.kernel.tenant_id, self.receipt.document_version_id)
        invalid = version.model_copy(update={"id": uuid4(), "version_number": 3,
                                              "parent_version_id": version.id, "content_sha256": "a" * 64})
        with self.assertRaises(TrustConflictError):
            self.repo.append_batch([invalid], self.kernel.actor)
        with self.psycopg.connect(RUNTIME_DSN) as connection:
            self.assertFalse(connection.execute("SELECT has_schema_privilege(current_user, 'citeprism', 'CREATE')").fetchone()[0])

    def test_phase2_reparse_preserves_both_extraction_observations(self):
        import json
        from tests.evidence_fixtures import scored, PDF
        receipt = self.kernel.ingest_pdf(PDF)
        artifact = scored(self.root)
        path = self.root / "phase2.json"
        path.write_text(json.dumps(artifact), encoding="utf-8")
        first = self.kernel.capture_legacy_scored_artifact(receipt.document_version_id, path)
        artifact["extraction_metadata"]["extraction_revision"] = "b" * 64
        artifact["scored_references"][0]["original_data"]["parsed"]["year"] = 2023
        path.write_text(json.dumps(artifact), encoding="utf-8")
        second = self.kernel.capture_legacy_scored_artifact(receipt.document_version_id, path)
        self.assertEqual(first.references_created, 10)
        self.assertEqual(second.references_created, 10)
        self.assertTrue(self.kernel.verify_audit_chain().valid)
