"""Durable queue, organizations, assignment, repairs, exports and telemetry.

These run against SQLite so they execute anywhere, including CI without a
database service. The PostgreSQL store satisfies the same contract and is
exercised separately by tests/test_phase4_postgres.py.
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from src.evidence import SCHEMA_VERSION
from src.workspace.common import WorkspaceConflict, WorkspaceNotFound
from src.workspace.models import AuditOptions
from src.workspace.repair import merge_options, plan_repairs
from src.workspace.service import WorkspaceService
from src.workspace.store import WorkspaceStore

TENANT_A = "00000000-0000-4000-8000-000000000001"
TENANT_B = "00000000-0000-4000-8000-000000000002"


def artifact(*, system_issue=True):
    """An artifact whose projection contains a repairable system issue."""
    assessments = (
        [{"task_id": "t1", "status": "assessment_unavailable", "error_code": "timeout"}]
        if system_issue
        else [
            {
                "task_id": "t1",
                "status": "grounded_proposal",
                "decision": "context_only",
                "validated_quotes": [{"text": "a located source quotation"}],
            }
        ]
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "manuscript_metadata": {"title": "A traceable paper", "authors": ["Ada Lovelace"]},
        "citations_in_text": [{"reference_ids": ["[1]"]}],
        "scored_references": [
            {
                "reference_uid": "ref-1",
                "ref_id": "[1]",
                "original_data": {
                    "raw_text": "[1] A source",
                    "parsed": {"title": "A source", "authors": ["Lovelace, A"], "year": 2025},
                    "extraction_status": "located",
                    "extraction_issues": [],
                    "source_locations": [{"page": 2}],
                },
                "resolution": {"status": "resolved", "metadata_differences": []},
                "external_metadata": {"id": "W1", "doi": "10.1/example"},
                "citation_count": 1,
                "evidence_status": (
                    "assessment_unavailable" if system_issue else "grounded_proposal"
                ),
                "claim_assessment": "not_assessed" if system_issue else "context_only",
                "claim_assessments": assessments,
            }
        ],
    }


class Phase4StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = WorkspaceStore(self.root / "workspace.db")
        self.service = WorkspaceService(self.store, data_root=self.root / "data")
        self.pdf = self.root / "paper.pdf"
        self.pdf.write_bytes(b"%PDF-1.7\nminimal test document")

    def tearDown(self):
        self.temp.cleanup()

    def queued_job(self, tenant=TENANT_A, **kwargs):
        manuscript, _ = self.service.register_pdf(
            tenant, self.pdf, original_filename="paper.pdf", actor_id="operator"
        )
        audit, job = self.store.create_audit_job(
            tenant, manuscript.id, {"pdf_path": str(self.pdf)}, "operator", **kwargs
        )
        return manuscript, audit, job

    # -- durable queue ------------------------------------------------
    def test_lease_is_single_winner_and_respects_priority(self):
        manuscript, _audit, low = self.queued_job(priority=200)
        _audit2, high = self.store.create_audit_job(
            TENANT_A, manuscript["id"] if isinstance(manuscript, dict) else manuscript.id,
            {"pdf_path": str(self.pdf)}, "operator", priority=10,
        )
        first = self.store.lease_next_job("worker-1")
        self.assertEqual(first["id"], high["id"], "the higher priority job must lease first")
        second = self.store.lease_next_job("worker-2")
        self.assertEqual(second["id"], low["id"])
        self.assertIsNone(self.store.lease_next_job("worker-3"), "no job may be leased twice")

    def test_expired_lease_requeues_then_dead_letters(self):
        _manuscript, _audit, job = self.queued_job(max_attempts=2)
        self.store.lease_next_job("worker-1", lease_seconds=1)
        time.sleep(1.1)
        self.assertEqual(self.store.reclaim_expired_leases(), 1)
        self.assertEqual(self.store.get_job(TENANT_A, job["id"])["state"], "queued")

        self.store.lease_next_job("worker-2", lease_seconds=1)
        time.sleep(1.1)
        self.store.reclaim_expired_leases()
        recovered = self.store.get_job(TENANT_A, job["id"])
        self.assertEqual(recovered["state"], "dead_letter")
        self.assertEqual(recovered["error_code"], "lease_expired")
        self.assertEqual(self.store.queue_depth(TENANT_A)["dead_letter"], 1)

    def test_heartbeat_holds_a_lease_and_only_for_its_owner(self):
        _manuscript, _audit, job = self.queued_job()
        self.store.lease_next_job("worker-1", lease_seconds=1)
        self.assertTrue(
            self.store.heartbeat_job(TENANT_A, job["id"], "worker-1", lease_seconds=30)
        )
        self.assertFalse(
            self.store.heartbeat_job(TENANT_A, job["id"], "impostor", lease_seconds=30),
            "a worker that does not hold the lease must not be able to renew it",
        )
        time.sleep(1.1)
        self.assertEqual(self.store.reclaim_expired_leases(), 0, "the renewed lease is still valid")

    def test_release_schedules_a_retry_then_fails_when_the_budget_is_spent(self):
        _manuscript, _audit, job = self.queued_job(max_attempts=2)
        self.store.lease_next_job("worker-1")
        outcome = self.store.release_job(
            TENANT_A, job["id"], "worker-1", error_code="rate_limit", backoff_seconds=0
        )
        self.assertEqual(outcome, "requeued")
        self.store.lease_next_job("worker-1")
        outcome = self.store.release_job(
            TENANT_A, job["id"], "worker-1", error_code="rate_limit", backoff_seconds=0
        )
        self.assertEqual(outcome, "failed")
        self.assertEqual(self.store.get_job(TENANT_A, job["id"])["state"], "failed")

    def test_backoff_defers_a_retry_beyond_the_next_poll(self):
        _manuscript, _audit, job = self.queued_job(max_attempts=3)
        self.store.lease_next_job("worker-1")
        self.store.release_job(
            TENANT_A, job["id"], "worker-1", error_code="timeout", backoff_seconds=60
        )
        self.assertIsNone(
            self.store.lease_next_job("worker-2"),
            "a job inside its backoff window must not be leased",
        )

    # -- organizations and assignment ---------------------------------
    def test_membership_roles_are_normalized_and_deactivation_is_recorded(self):
        self.store.ensure_organization(TENANT_A, "Editorial office", actor_id="admin")
        member = self.store.upsert_member(
            TENANT_A, "reviewer@example.org", display_name="A Reviewer",
            email="reviewer@example.org", roles=["Reviewer", "operator"], actor_id="admin",
        )
        self.assertEqual(member["roles"], ["operator", "reviewer"])
        self.assertTrue(member["active"])
        self.assertFalse(self.store.deactivate_member(TENANT_A, member["subject"], "admin")["active"])
        with self.assertRaises(WorkspaceNotFound):
            self.store.deactivate_member(TENANT_A, "nobody@example.org", "admin")

    def test_assignment_tracks_workload_and_refuses_inactive_members(self):
        _manuscript, audit, _job = self.queued_job()
        self.store.upsert_member(
            TENANT_A, "reviewer@example.org", display_name=None, email=None,
            roles=["reviewer"], actor_id="admin",
        )
        self.store.assign_case(
            TENANT_A, audit["id"], "finding-1", "reviewer@example.org", actor_id="admin"
        )
        workload = {item["subject"]: item["open_cases"] for item in
                    self.store.assignment_workload(TENANT_A)}
        self.assertEqual(workload["reviewer@example.org"], 1)

        self.store.unassign_case(TENANT_A, audit["id"], "finding-1", "admin")
        workload = {item["subject"]: item["open_cases"] for item in
                    self.store.assignment_workload(TENANT_A)}
        self.assertEqual(workload["reviewer@example.org"], 0)

        self.store.deactivate_member(TENANT_A, "reviewer@example.org", "admin")
        with self.assertRaises(WorkspaceConflict):
            self.store.assign_case(
                TENANT_A, audit["id"], "finding-2", "reviewer@example.org", actor_id="admin"
            )

    def test_assignment_is_tenant_scoped(self):
        _manuscript, audit, _job = self.queued_job()
        self.assertEqual(self.store.list_assignments(TENANT_B, audit_id=audit["id"]), [])

    # -- repairs -------------------------------------------------------
    def test_repair_plans_target_only_system_issues(self):
        from src.evidence.review import build_review

        review = build_review(artifact(system_issue=True))
        plans = plan_repairs(review)
        self.assertTrue(plans, "an assessment failure must produce a repair plan")
        self.assertEqual(plans[0].issue_category, "assessment_error")
        self.assertEqual(plans[0].action, "rerun_assessment")
        # The cheapest repair reuses cached parsing and enrichment.
        self.assertFalse(plans[0].options["force_parse"])
        self.assertFalse(plans[0].options["force_enrich"])
        self.assertTrue(plans[0].options["force_score"])

        clean = plan_repairs(build_review(artifact(system_issue=False)))
        self.assertEqual(clean, [], "a clean audit must not offer repairs")

    def test_merged_repair_options_take_the_union_of_required_stages(self):
        from src.evidence.review import build_review

        plans = plan_repairs(build_review(artifact(system_issue=True)))
        merged = merge_options(plans)
        self.assertTrue(merged["force_score"])
        self.assertFalse(merged["run_missing_citations"])

    def test_repair_records_are_idempotent(self):
        _manuscript, audit, _job = self.queued_job()
        first = self.store.record_repair(
            TENANT_A, audit_id=audit["id"], finding_id="f1",
            issue_category="assessment_error", action="rerun_assessment",
            state="queued", detail={}, requested_by="reviewer", idempotency_key="repair-1",
        )
        replay = self.store.record_repair(
            TENANT_A, audit_id=audit["id"], finding_id="f1",
            issue_category="assessment_error", action="rerun_assessment",
            state="queued", detail={}, requested_by="reviewer", idempotency_key="repair-1",
        )
        self.assertEqual(first["id"], replay["id"])
        updated = self.store.update_repair(TENANT_A, first["id"], state="succeeded")
        self.assertEqual(updated["state"], "succeeded")
        self.assertIsNotNone(updated["completed_at"])

    # -- exports and telemetry ----------------------------------------
    def test_export_records_carry_their_signature_and_expiry(self):
        _manuscript, audit, _job = self.queued_job()
        record = self.store.record_export(
            TENANT_A, audit_id=audit["id"], format="bundle", object_backend="local",
            object_key="k", content_sha256="a" * 64, size_bytes=2048,
            signature_algorithm="ed25519", signature_key_id="k1", signature="sig",
            manifest={"files": []}, created_by="reviewer", retention_days=30,
        )
        self.assertEqual(record["format"], "bundle")
        self.assertIsNotNone(record["expires_at"])
        self.assertEqual(self.store.get_export(TENANT_A, record["id"])["id"], record["id"])
        with self.assertRaises(WorkspaceNotFound):
            self.store.get_export(TENANT_B, record["id"])

    def test_telemetry_and_latency_windows(self):
        _manuscript, audit, job = self.queued_job()
        self.store.lease_next_job("worker-1")
        self.store.finish_job(
            TENANT_A, job["id"], success=True, artifact_path=None,
            summary={"total_references": 20, "resolved_references": 18,
                     "system_issues": 1, "findings": 4},
        )
        self.store.record_telemetry(TENANT_A, [
            {"stage": "parse", "duration_ms": 4200, "provider": "google",
             "model": "gemini-2.5-flash", "input_tokens": 50_000, "output_tokens": 4_000,
             "requests": 1, "cost_usd": 0.025, "audit_id": audit["id"]},
        ])
        events = self.store.telemetry_window(TENANT_A, hours=1)
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(events[0]["cost_usd"], 0.025)
        self.assertEqual(self.store.telemetry_window(TENANT_B, hours=1), [])

        latency = self.store.audit_latency_window(TENANT_A, hours=24)
        self.assertEqual(len(latency), 1)
        self.assertEqual(latency[0]["state"], "succeeded")
        self.assertEqual(latency[0]["summary"]["total_references"], 20)

    def test_activity_chain_survives_every_phase4_write(self):
        _manuscript, audit, job = self.queued_job()
        self.store.ensure_organization(TENANT_A, "Editorial office", actor_id="admin")
        self.store.upsert_member(TENANT_A, "r@example.org", display_name=None, email=None,
                                 roles=["reviewer"], actor_id="admin")
        self.store.assign_case(TENANT_A, audit["id"], "f1", "r@example.org", actor_id="admin")
        self.store.record_repair(
            TENANT_A, audit_id=audit["id"], finding_id="f1", issue_category="assessment_error",
            action="rerun_assessment", state="queued", detail={}, requested_by="admin",
        )
        self.store.record_export(
            TENANT_A, audit_id=audit["id"], format="json", object_backend="local",
            object_key="k", content_sha256="b" * 64, size_bytes=1,
            signature_algorithm="ed25519", signature_key_id="k1", signature="s",
            manifest={}, created_by="admin",
        )
        self.store.lease_next_job("worker-1")
        verification = self.store.verify_activity_chain(TENANT_A)
        self.assertTrue(verification["valid"])
        self.assertGreaterEqual(verification["event_count"], 7)

        with self.store.connection() as connection:
            connection.execute(
                "UPDATE workspace_activity SET action='tampered' "
                "WHERE tenant_id=? AND id=(SELECT MIN(id) FROM workspace_activity WHERE tenant_id=?)",
                (TENANT_A, TENANT_A),
            )
        self.assertFalse(self.store.verify_activity_chain(TENANT_A)["valid"])

    def test_phase3_behaviour_is_unchanged(self):
        """The Phase 3 claim/finish path must still work exactly as before."""
        manuscript, audit, job = self.queued_job()
        self.assertIsNotNone(self.store.claim_job(TENANT_A, job["id"], "worker-1"))
        self.assertIsNone(self.store.claim_job(TENANT_A, job["id"], "worker-2"))
        output = self.root / "data" / "scored" / "artifact.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(artifact()), encoding="utf-8")
        self.store.finish_job(
            TENANT_A, job["id"], success=True, artifact_path=str(output),
            summary={"references_to_review": 1},
        )
        state = self.service.get_audit(TENANT_A, audit["id"]).state
        self.assertEqual(getattr(state, "value", state), "completed_with_issues")
        _created = self.service.create_audit(
            TENANT_A, manuscript.id, AuditOptions(), "operator"
        )


if __name__ == "__main__":
    unittest.main()
