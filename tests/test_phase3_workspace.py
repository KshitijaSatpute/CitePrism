"""Offline acceptance tests for Phase 3 workflow state and job recovery."""

import json
import tempfile
import unittest
from pathlib import Path

from src.evidence import SCHEMA_VERSION
from src.workspace.jobs import AuditJobRunner
from src.workspace.models import AuditOptions, CorrectionCreate, DecisionCreate
from src.workspace.service import WorkspaceService
from src.workspace.store import WorkspaceConflict, WorkspaceNotFound, WorkspaceStore

TENANT_A = "00000000-0000-4000-8000-000000000001"
TENANT_B = "00000000-0000-4000-8000-000000000002"


def artifact():
    return {
        "schema_version": SCHEMA_VERSION,
        "manuscript_metadata": {"title": "A traceable paper"},
        "citations_in_text": [{"reference_ids": ["[1]"]}],
        "scored_references": [
            {
                "reference_uid": "ref-1",
                "ref_id": "[1]",
                "original_data": {
                    "parsed": {"title": "A source", "authors": ["Reviewer"], "year": 2025},
                    "extraction_status": "located",
                    "extraction_issues": [],
                    "source_locations": [{"page": 2}],
                },
                "resolution": {"status": "resolved", "metadata_differences": []},
                "external_metadata": {"id": "W1", "doi": "10.1/example"},
                "citation_count": 1,
                "evidence_status": "grounded_proposal",
                "claim_assessment": "context_only",
                "claim_assessments": [
                    {
                        "task_id": "task-1",
                        "status": "grounded_proposal",
                        "decision": "context_only",
                        "validated_quotes": [{"text": "source quotation"}],
                    }
                ],
            }
        ],
    }


class FakePipeline:
    def __init__(self, output: Path, *, success=True):
        self.output, self.success = output, success
        self.calls = []

    def process_document(self, path, progress_bar=None, **options):
        self.calls.append((Path(path), options))
        progress_bar.progress(0.4, text="Resolving sources")
        if not self.success:
            return {"success": False, "errors": ["temporary provider failure"]}
        self.output.write_text(json.dumps(artifact()), encoding="utf-8")
        progress_bar.progress(0.9, text="Building evidence")
        return {
            "success": True,
            "file_paths": {"scored": str(self.output)},
            "stages_completed": ["evidence_review"],
            "errors": [],
        }


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = WorkspaceStore(self.root / "workspace.db")
        self.service = WorkspaceService(self.store, data_root=self.root / "data")
        self.pdf = self.root / "paper.pdf"
        self.pdf.write_bytes(b"%PDF-1.7\nminimal test document\n%%EOF\n")

    def tearDown(self):
        self.temp.cleanup()

    def manuscript(self, tenant=TENANT_A):
        return self.service.register_pdf(
            tenant, self.pdf, original_filename="paper.pdf", actor_id="reviewer-a"
        )[0]

    def complete_audit(self):
        manuscript = self.manuscript()
        audit, job = self.service.create_audit(
            TENANT_A, manuscript.id, AuditOptions(), "reviewer-a"
        )
        self.store.claim_job(TENANT_A, job.id, "worker-1")
        output = self.root / "data" / "scored" / "artifact.json"
        output.parent.mkdir(parents=True)
        output.write_text(json.dumps(artifact()), encoding="utf-8")
        review = __import__("src.evidence.review", fromlist=["build_review"]).build_review(
            artifact()
        )
        self.store.finish_job(
            TENANT_A,
            job.id,
            success=True,
            artifact_path=str(output),
            summary=review["counts"],
            result={"ok": True},
        )
        return manuscript, audit, job

    def test_upload_is_content_addressed_idempotent_and_tenant_scoped(self):
        first, created = self.service.register_pdf(
            TENANT_A, self.pdf, original_filename="unsafe ../ paper.pdf", actor_id="reviewer-a"
        )
        again, duplicated = self.service.register_pdf(
            TENANT_A, self.pdf, original_filename="renamed.pdf", actor_id="reviewer-a"
        )
        other, other_created = self.service.register_pdf(
            TENANT_B, self.pdf, original_filename="paper.pdf", actor_id="reviewer-b"
        )
        self.assertTrue(created)
        self.assertFalse(duplicated)
        self.assertTrue(other_created)
        self.assertEqual(first.id, again.id)
        self.assertNotEqual(first.id, other.id)
        self.assertEqual(len(self.service.list_manuscripts(TENANT_A)), 1)
        with self.assertRaises(WorkspaceNotFound):
            self.service.get_manuscript(TENANT_B, first.id)

    def test_job_recovery_and_claim_are_single_winner(self):
        manuscript = self.manuscript()
        _audit, job = self.service.create_audit(TENANT_A, manuscript.id, AuditOptions(), "operator")
        claimed = self.store.claim_job(TENANT_A, job.id, "worker-before-crash")
        self.assertEqual(claimed["attempts"], 1)
        self.assertEqual(self.store.recover_interrupted_jobs(), 1)
        claimed = self.store.claim_job(TENANT_A, job.id, "worker-after-restart")
        self.assertEqual(claimed["attempts"], 2)
        self.assertIsNone(self.store.claim_job(TENANT_A, job.id, "other-worker"))

    def test_runner_publishes_review_and_machine_progress(self):
        manuscript = self.manuscript()
        audit, job = self.service.create_audit(TENANT_A, manuscript.id, AuditOptions(), "operator")
        output = self.root / "data" / "scored" / "runner.json"
        output.parent.mkdir(parents=True)
        runner = AuditJobRunner(self.store, FakePipeline(output), max_workers=1)
        runner.submit(TENANT_A, job.id)
        runner.shutdown()
        final = self.service.get_job(TENANT_A, job.id)
        finished = self.service.get_audit(TENANT_A, audit.id)
        self.assertEqual(final.state.value, "succeeded")
        self.assertEqual(final.progress, 100)
        self.assertEqual(finished.state.value, "completed_with_issues")
        self.assertEqual(self.service.review(TENANT_A, audit.id)["counts"]["grounded_proposals"], 1)

    def test_a_later_audit_of_the_same_paper_leaves_the_earlier_results_alone(self):
        # The pipeline writes one scored file per document; each audit must keep its own copy.
        manuscript = self.manuscript()
        output = self.root / "data" / "scored" / "same_document.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        first, job = self.service.create_audit(TENANT_A, manuscript.id, AuditOptions(), "operator")
        runner = AuditJobRunner(self.store, FakePipeline(output), max_workers=1)
        runner.submit(TENANT_A, job.id)
        runner.shutdown()
        before = self.service.review(TENANT_A, first.id)
        stored = self.service.get_audit(TENANT_A, first.id).artifact_path
        self.assertIn("audit_artifacts", stored)
        self.assertNotEqual(Path(stored).resolve(), output.resolve())

        class ChangedPipeline(FakePipeline):
            def process_document(self, path, progress_bar=None, **options):
                result = super().process_document(path, progress_bar, **options)
                data = json.loads(self.output.read_text(encoding="utf-8"))
                data["scored_references"][0]["original_data"]["parsed"]["title"] = "A different later result"
                self.output.write_text(json.dumps(data), encoding="utf-8")
                return result

        second, job = self.service.create_audit(TENANT_A, manuscript.id, AuditOptions(), "operator")
        runner = AuditJobRunner(self.store, ChangedPipeline(output), max_workers=1)
        runner.submit(TENANT_A, job.id)
        runner.shutdown()
        self.assertEqual(self.service.review(TENANT_A, first.id), before)
        self.assertNotEqual(self.service.get_audit(TENANT_A, second.id).artifact_path, stored)

    def test_decisions_are_idempotent_append_only_and_optimistic(self):
        _manuscript, audit, _job = self.complete_audit()
        finding = self.service.review(TENANT_A, audit.id)["findings"][0]
        first = self.service.record_decision(
            TENANT_A,
            audit.id,
            finding["id"],
            DecisionCreate(
                disposition="confirm", rationale="Checked against the supplied evidence."
            ),
            "reviewer-a",
            "decision-key-0001",
        )
        replay = self.service.record_decision(
            TENANT_A,
            audit.id,
            finding["id"],
            DecisionCreate(
                disposition="confirm", rationale="Checked against the supplied evidence."
            ),
            "reviewer-a",
            "decision-key-0001",
        )
        self.assertEqual(first.id, replay.id)
        with self.assertRaises(WorkspaceConflict):
            self.service.record_decision(
                TENANT_A,
                audit.id,
                finding["id"],
                DecisionCreate(disposition="dismiss", rationale="A concurrent stale edit."),
                "reviewer-b",
                "decision-key-0002",
            )
        replacement = self.service.record_decision(
            TENANT_A,
            audit.id,
            finding["id"],
            DecisionCreate(
                disposition="dismiss",
                rationale="Second review changed the disposition.",
                expected_current_decision_id=first.id,
            ),
            "reviewer-b",
            "decision-key-0003",
        )
        self.assertEqual(replacement.supersedes_decision_id, first.id)
        projected = self.service.review(TENANT_A, audit.id)
        self.assertEqual(projected["review_status"], "in_review")
        self.assertEqual(projected["counts"]["reviewer_decisions"], 1)

    def test_corrections_preserve_original_evidence_and_activity(self):
        _manuscript, audit, _job = self.complete_audit()
        reference = self.service.review(TENANT_A, audit.id)["references"][0]
        correction = self.service.propose_correction(
            TENANT_A,
            audit.id,
            CorrectionCreate(
                kind="metadata",
                reference_ids=[reference["id"]],
                proposed_value={"title": "Corrected title"},
                rationale="Compared with the publisher record.",
            ),
            "reviewer-a",
            "correction-key-1",
        )
        accepted = self.store.transition_correction(
            TENANT_A, correction.id, "accepted", "Approved by the assigned reviewer.", "reviewer-b"
        )
        self.assertEqual(accepted["state"], "accepted")
        self.assertEqual(
            artifact()["scored_references"][0]["original_data"]["parsed"]["title"], "A source"
        )
        actions = [item.action for item in self.service.activity(TENANT_A, audit.id)]
        self.assertIn("correction.proposed", actions)
        self.assertIn("correction.accepted", actions)
        self.assertTrue(self.store.verify_activity_chain(TENANT_A)["valid"])
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE workspace_activity SET action='tampered' WHERE tenant_id=? AND id=(SELECT MIN(id) FROM workspace_activity WHERE tenant_id=?)",
                (TENANT_A, TENANT_A),
            )
        self.assertFalse(self.store.verify_activity_chain(TENANT_A)["valid"])


if __name__ == "__main__":
    unittest.main()
