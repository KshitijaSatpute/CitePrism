import base64
import hashlib
import io
import json
import os
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

from pydantic import ValidationError

from config.trust import env_bool
from src.trust.audit import verify_audit_chain
from src.trust.canonical import canonical_json, sha256_json, sha256_text
from src.trust.factory import build_trust_kernel
from src.trust.ids import derived_uuid, new_uuid7, utc_now
from src.trust.memory_repository import InMemoryTrustRepository
from src.trust.migrations import default_migrations_dir
from src.trust.models import (Actor, DocumentRecord, Evidence, ModelRun, ReferenceRecord,
                              SourceSpan, AuditEvent)
from src.trust.object_store import LocalObjectStore, S3ObjectStore, object_key_for_digest
from src.trust.ports import TrustConfigurationError, TrustConflictError, TrustIntegrityError
from src.trust.postgres_repository import PostgresTrustRepository
from src.trust.schema import MODELS, RUNTIME_TABLES
from src.trust.service import safe_filename
from tests.trust_fixtures import make_kernel, make_pdf, make_scored


class TrustTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = InMemoryTrustRepository()
        self.kernel = make_kernel(self.root, self.repo)
        self.pdf = make_pdf(self.root)
        self.receipt = self.kernel.ingest_pdf(self.pdf, external_key="case-1")

    def snapshot(self):
        return self.repo.audit_snapshot(self.kernel.tenant_id)

    def records(self, kind):
        return [r for (k, _), (r, _) in self.snapshot()[1].items() if k == kind]

    def capture(self):
        return self.kernel.capture_legacy_scored_artifact(self.receipt.document_version_id, make_scored(self.root))

    def test_ingest_deduplicates_identical_bytes_without_more_events(self):
        result = self.kernel.ingest_pdf(self.pdf, original_filename="renamed.pdf", external_key="case-1")
        self.assertTrue(result.reused)
        self.assertEqual(result.document_version_id, self.receipt.document_version_id)
        self.assertEqual(len(self.snapshot()[0]), 2)
        self.assertTrue(self.kernel.verify_audit_chain().valid)

    def test_ingest_preserves_revision_parent_and_rejects_rebinding(self):
        result = self.kernel.ingest_pdf(make_pdf(self.root, "second.pdf", b"second"), external_key="case-1")
        version = self.repo.get_record("document_version", self.kernel.tenant_id, result.document_version_id)
        self.assertEqual(version.version_number, 2)
        self.assertEqual(version.parent_version_id, self.receipt.document_version_id)
        self.assertEqual(result.document_id, self.receipt.document_id)
        with self.assertRaises(TrustConflictError):
            self.kernel.ingest_pdf(self.pdf, external_key="different-case")

    def test_tenant_read_and_relationship_isolation(self):
        other = make_kernel(self.root, self.repo)
        self.assertIsNone(self.repo.get_record("document_version", other.tenant_id, self.receipt.document_version_id))
        with self.assertRaises(TrustConflictError):
            other.record_source_span(document_version_id=self.receipt.document_version_id, text="text", page_start=1, page_end=1)
        self.assertTrue(other.verify_audit_chain().valid)

    def test_batch_failure_rolls_back_records_and_audit(self):
        tenant = self.kernel.tenant_id
        doc = DocumentRecord(id=uuid4(), tenant_id=tenant, created_at=utc_now(), created_by="test")
        ref = ReferenceRecord(id=uuid4(), tenant_id=tenant, document_version_id=uuid4(),
                              reference_key="bad", ordinal=1, raw_text_sha256="a"*64, created_at=utc_now())
        before = self.snapshot()
        with self.assertRaises(TrustConflictError):
            self.repo.append_batch([doc, ref], self.kernel.actor)
        self.assertEqual(self.snapshot(), before)

    def test_mixed_tenant_batch_rejected(self):
        doc = self.records("document")[0]
        with self.assertRaises(TrustConflictError):
            self.repo.append_batch([doc, doc.model_copy(update={"tenant_id": uuid4()})], self.kernel.actor)

    def test_immutable_id_rejects_changed_content(self):
        doc = self.records("document")[0]
        with self.assertRaises(TrustConflictError):
            self.repo.append_batch([doc.model_copy(update={"title": "Changed"})], self.kernel.actor)

    def test_concurrent_same_file_ingestion_is_idempotent(self):
        fresh = make_pdf(self.root, "concurrent.pdf", b"concurrent")
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.kernel.ingest_pdf(fresh), range(12)))
        self.assertEqual(len({r.document_version_id for r in results}), 1)
        self.assertTrue(self.kernel.verify_audit_chain().valid)

    def test_capture_preserves_artifact_and_unknown_confidence(self):
        result = self.capture()
        self.assertEqual((result.references_created, result.candidate_matches_created,
                          result.evidence_created, result.findings_created), (1, 1, 1, 2))
        self.assertIsNone(self.records("candidate_match")[0].confidence)
        run = self.records("model_run")[0]
        self.assertEqual(run.purpose, "legacy_score_import")
        self.assertFalse(run.configuration["provider_execution_observed"])
        stored = self.root / "objects" / run.configuration["artifact_object"]["key"]
        self.assertEqual(stored.read_bytes(), (self.root / "scored.json").read_bytes())
        self.assertEqual(hashlib.sha256(stored.read_bytes()).hexdigest(), run.output_sha256)
        self.assertTrue(self.kernel.verify_audit_chain().valid)

    def test_capture_retry_adds_no_entities_or_events(self):
        first = self.capture()
        before = self.snapshot()
        second = self.capture()
        self.assertEqual(first.model_run_id, second.model_run_id)
        self.assertEqual(second.references_created + second.candidate_matches_created + second.evidence_created + second.findings_created, 0)
        self.assertEqual(self.snapshot(), before)

    def test_invalid_capture_and_changed_reference_do_not_partially_commit(self):
        before = self.snapshot()
        invalid = make_scored(self.root, references=[{"ref_id": "1"}, {"ref_id": "[1]"}])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.kernel.capture_legacy_scored_artifact(self.receipt.document_version_id, invalid)
        self.assertEqual(self.snapshot(), before)
        self.capture()
        before = self.snapshot()
        invalid = make_scored(self.root, references=[{"ref_id": "[1]", "original_data": {"parsed": {"title": "Changed"}}}])
        with self.assertRaises(TrustConflictError):
            self.kernel.capture_legacy_scored_artifact(self.receipt.document_version_id, invalid)
        self.assertEqual(self.snapshot(), before)

    def test_missing_and_out_of_range_scores_are_not_clean(self):
        path = make_scored(self.root, references=[{"ref_id": str(i), "RS_final": value}
                                               for i, value in enumerate([None, True, -1, 101, "bad"], 1)])
        self.kernel.capture_legacy_scored_artifact(self.receipt.document_version_id, path)
        self.assertEqual([f.category for f in self.records("finding")], ["scoring_unavailable"] * 5)

    def test_decisions_are_append_only_and_supersession_cannot_branch(self):
        self.capture()
        finding = self.records("finding")[0]
        first = self.kernel.record_reviewer_decision(finding_id=finding.id, disposition="needs_more_evidence",
                                                     rationale="Fetch original source", reviewer_id="reviewer-1")
        second = self.kernel.record_reviewer_decision(finding_id=finding.id, disposition="dismiss",
                                                      rationale="Source located", reviewer_id="reviewer-1",
                                                      supersedes_decision_id=first.id)
        with self.assertRaises(TrustConflictError):
            self.kernel.record_reviewer_decision(finding_id=finding.id, disposition="confirm",
                                                 rationale="Fork", reviewer_id="reviewer-2", supersedes_decision_id=first.id)
        self.assertEqual(second.supersedes_decision_id, first.id)
        self.assertEqual(self.records("finding")[0].status.value, "open")
        self.assertTrue(self.kernel.verify_audit_chain().valid)

    def test_cross_document_evidence_is_rejected(self):
        self.capture()
        other = self.kernel.ingest_pdf(make_pdf(self.root, "other.pdf", b"other"))
        evidence = Evidence(id=uuid4(), tenant_id=self.kernel.tenant_id, document_version_id=other.document_version_id,
                            subject_type="reference", subject_id=self.records("reference")[0].id,
                            kind="metadata_match", statement="wrong document", created_at=utc_now())
        with self.assertRaises(TrustConflictError):
            self.repo.append_batch([evidence], self.kernel.actor)

    def test_source_span_bounds_and_digest(self):
        span = self.kernel.record_source_span(document_version_id=self.receipt.document_version_id,
                                              text="source text", page_start=1, page_end=1)
        self.assertEqual(span.text_sha256, sha256_text("source text"))
        with self.assertRaises(ValidationError):
            SourceSpan.model_validate({**span.model_dump(), "char_start": 5, "char_end": 4})
        with self.assertRaises(ValidationError):
            SourceSpan.model_validate({**span.model_dump(), "page_end": None})

    def test_audit_detects_tamper_gaps_missing_and_unaudited_records(self):
        events, records = self.snapshot()
        changed = events[0].model_copy(update={"action": "tampered"})
        self.assertFalse(verify_audit_chain([changed, *events[1:]], records=records).valid)
        self.assertFalse(verify_audit_chain(events[1:], records=records).valid)
        self.assertFalse(verify_audit_chain(events, records={}).valid)
        self.assertFalse(verify_audit_chain([], records=records).valid)
        key = next(iter(records))
        modified = dict(records)
        modified[key] = (records[key][0].model_copy(update={"created_by": "tampered"}), records[key][1])
        self.assertFalse(verify_audit_chain(events, records=modified).valid)
        self.assertFalse(verify_audit_chain(events, tenant_id=uuid4(), records=records).valid)

    def test_models_forbid_naive_time_extra_fields_and_nested_mutation(self):
        doc = self.records("document")[0]
        with self.assertRaises(ValidationError):
            DocumentRecord.model_validate({**doc.model_dump(), "created_at": datetime.now()})
        with self.assertRaises(ValidationError):
            DocumentRecord.model_validate({**doc.model_dump(), "surprise": True})
        self.capture()
        run = self.records("model_run")[0]
        with self.assertRaises(TypeError):
            run.configuration["legacy_metadata"]["generated_at"] = "tampered"
        with self.assertRaises(ValidationError):
            run.model = "tampered"
        self.assertEqual(sha256_json(run), sha256_json(ModelRun.model_validate_json(run.model_dump_json())))

    def test_run_completion_contract(self):
        self.capture()
        value = self.records("model_run")[0].model_dump()
        for change in ({"output_sha256": None}, {"status": "started"},
                       {"completed_at": value["started_at"] - timedelta(seconds=1)},
                       {"status": "failed", "error_code": None}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                ModelRun.model_validate({**value, **change})

    def test_local_store_never_overwrites_corrupted_existing_key(self):
        target = self.root / "objects" / self.receipt.object_key
        target.write_bytes(b"corrupted")
        with self.assertRaises(TrustIntegrityError):
            self.kernel.ingest_pdf(self.pdf, external_key="case-1")
        self.assertEqual(target.read_bytes(), b"corrupted")

    def test_safe_names_and_object_paths(self):
        self.assertEqual(safe_filename(r"C:\secret\paper.pdf"), "paper.pdf")
        for prefix in ("../escape", "/absolute", "a/../../b", r"a\b"):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                object_key_for_digest("a" * 64, prefix)
        with self.assertRaises(ValueError):
            object_key_for_digest("not-a-hash")

    def test_pdf_validation_and_mismatched_digest_fail(self):
        bad = self.root / "fake.pdf"
        bad.write_bytes(b"not a PDF")
        with self.assertRaises(ValueError):
            self.kernel.ingest_pdf(bad)
        with self.assertRaises(TrustIntegrityError):
            LocalObjectStore(self.root / "other").put_file(self.pdf, content_sha256="a" * 64, media_type="application/pdf")

    def test_pipeline_passes_original_name_to_both_stores(self):
        from pipeline_orchestrator import PipelineOrchestrator
        from tests.test_phase0 import FakeDatabase
        config = SimpleNamespace(RAW_PDFS_DIR=self.root / "raw", PARSED_DIR=self.root / "parsed",
                                 ENRICHED_DIR=self.root / "enriched", SCORED_DIR=self.root / "scored", MAX_PDF_BYTES=1024)
        kernel = make_kernel(self.root, InMemoryTrustRepository())
        result = PipelineOrchestrator(FakeDatabase(), config, kernel)._stage_register_pdf(self.pdf, original_filename="Original.pdf")
        self.assertTrue(result["success"])
        version = kernel.repository.get_record("document_version", kernel.tenant_id, UUID(result["trust"]["document_version_id"]))
        self.assertEqual(version.original_filename, "Original.pdf")


class ContractTests(unittest.TestCase):
    def test_uuid7_version_time_and_derived_parts(self):
        now = utc_now()
        value = new_uuid7(now)
        self.assertEqual(value.version, 7)
        self.assertEqual(value.int >> 80, int(now.timestamp() * 1000))
        self.assertNotEqual(derived_uuid(value, "a:b", "c"), derived_uuid(value, "a", "b:c"))

    def test_canonical_order_and_nonfinite_rejection(self):
        self.assertEqual(canonical_json({"b": 1, "a": 2}), canonical_json({"a": 2, "b": 1}))
        with self.assertRaises(ValueError):
            canonical_json({"number": float("nan")})
        self.assertEqual(canonical_json({"zero": -0.0}), canonical_json({"zero": 0.0}))

    def test_nested_nonfinite_model_payload_is_rejected(self):
        with self.assertRaises(ValidationError):
            SourceSpan(id=uuid4(), tenant_id=uuid4(), document_version_id=uuid4(),
                       text_sha256="a" * 64, page_start=1, page_end=1, created_at=utc_now(),
                       locator={"nested": [float("nan")]})

    def test_strict_booleans_and_disabled_factory(self):
        with patch.dict(os.environ, {"TEST_BOOLEAN": "false"}):
            self.assertFalse(env_bool("TEST_BOOLEAN"))
        with patch.dict(os.environ, {"TEST_BOOLEAN": "typo"}):
            with self.assertRaises(ValueError):
                env_bool("TEST_BOOLEAN")
        self.assertIsNone(build_trust_kernel(SimpleNamespace(TRUST_KERNEL_ENABLED=False)))

    def test_schema_columns_match_models(self):
        source = (default_migrations_dir() / "001_trust_kernel.sql").read_text(encoding="utf-8")
        for table, model in [*MODELS.values(), ("audit_events", AuditEvent)]:
            with self.subTest(table=table):
                body = re.search(r"CREATE TABLE citeprism\." + table + r" \((.*?)\n\);", source, re.S).group(1)
                columns = set(re.findall(r"^    ([a-z_][a-z_0-9]*) (?:UUID|SMALLINT|TEXT|TIMESTAMPTZ|INTEGER|BIGINT|JSONB|DOUBLE|BOOLEAN)\b", body, re.M))
                expected = set(model.model_fields) | (set() if model is AuditEvent else {"record_hash"})
                self.assertEqual(columns, expected)

    def test_runtime_role_checks_fail_closed(self):
        class Cursor:
            def __init__(self, replies): self.replies = iter(replies)
            def execute(self, *args): pass
            def fetchone(self): return next(self.replies)
        repo = PostgresTrustRepository("postgresql://example", expected_role="app")
        role = {"name": "app", "rolcanlogin": True, "privileged": False}
        permissions = {"table_count": len(RUNTIME_TABLES), "unsafe": False}
        repo._assert_runtime(Cursor([role, permissions, {"required": True}]))
        for replies in ([{**role, "privileged": True}], [{**role, "name": "owner"}],
                        [role, {**permissions, "unsafe": True}],
                        [role, permissions, {"required": False}]):
            with self.subTest(replies=replies), self.assertRaises(TrustConfigurationError):
                repo._assert_runtime(Cursor(replies))


class S3Error(Exception):
    def __init__(self, code): self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self):
        self.head = None
        self.content = b""
        self.puts = []
        self.gets = []
        self.enabled = True
        self.race = False

    def get_bucket_versioning(self, **kwargs):
        return {"Status": "Enabled" if self.enabled else "Suspended"}

    def head_object(self, **kwargs):
        if self.head is None: raise S3Error("404")
        if "VersionId" in kwargs: assert kwargs["VersionId"] == self.head["VersionId"]
        return dict(self.head)

    def put_object(self, Body, **kwargs):
        self.puts.append(kwargs)
        self.content = Body.read()
        self.head = {"VersionId": "version-1", "ContentLength": len(self.content),
                     "Metadata": kwargs["Metadata"], "ChecksumSHA256": kwargs["ChecksumSHA256"],
                     "ServerSideEncryption": kwargs["ServerSideEncryption"]}
        if self.race: raise S3Error("412")
        return {"VersionId": "version-1"}

    def get_object(self, **kwargs):
        self.gets.append(kwargs)
        return {"Body": io.BytesIO(self.content)}


class S3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = make_pdf(self.temp.name)
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.client = FakeS3()
        self.store = S3ObjectStore(bucket="test", client=self.client)

    def put(self):
        return self.store.put_file(self.path, content_sha256=self.digest, media_type="application/pdf")

    def test_s3_create_only_put_and_reuse(self):
        self.assertEqual(self.put().version_id, "version-1")
        self.put()
        self.assertEqual(len(self.client.puts), 1)
        self.assertEqual(self.client.puts[0]["IfNoneMatch"], "*")
        self.assertEqual(self.client.puts[0]["ChecksumSHA256"], base64.b64encode(bytes.fromhex(self.digest)).decode())

    def test_s3_conditional_race_verified(self):
        self.client.race = True
        self.assertEqual(self.put().content_sha256, self.digest)

    def test_s3_unversioned_bucket_rejected(self):
        self.client.enabled = False
        with self.assertRaises(TrustConfigurationError):
            S3ObjectStore(bucket="test", client=self.client)

    def test_s3_bad_version_encryption_size_and_checksum_rejected(self):
        self.put()
        original = dict(self.client.head)
        for change in ({"VersionId": "null"}, {"ServerSideEncryption": "none"},
                       {"ContentLength": 999}, {"Metadata": {}}, {"ChecksumSHA256": "wrong"}):
            self.client.head = {**original, **change}
            with self.subTest(change=change), self.assertRaises(TrustIntegrityError):
                self.put()

    def test_s3_missing_checksum_rehashes_pinned_version(self):
        self.put()
        self.client.head.pop("ChecksumSHA256")
        self.put()
        self.assertEqual(self.client.gets[-1]["VersionId"], "version-1")
        self.client.content = b"corrupt"
        with self.assertRaises(TrustIntegrityError):
            self.put()

    def test_s3_access_denied_is_not_treated_as_missing(self):
        with patch.object(self.client, "head_object", side_effect=S3Error("AccessDenied")):
            with self.assertRaises(S3Error): self.put()
        self.assertEqual(self.client.puts, [])
