"""PostgreSQL workspace tests.

Skipped unless CITEPRISM_TEST_WORKSPACE_URL points at a disposable database that
has already had the workspace migrations applied. `verify_phase4.ps1
-WithPostgres` and the CI workflow both set this up.
"""

import os
import time
import unittest
import uuid

DSN = os.getenv("CITEPRISM_TEST_WORKSPACE_URL")
SCHEMA = os.getenv("CITEPRISM_TEST_WORKSPACE_SCHEMA", "citeprism_workspace")


@unittest.skipUnless(DSN, "CITEPRISM_TEST_WORKSPACE_URL is not configured")
class PostgresWorkspaceTests(unittest.TestCase):
    def setUp(self):
        from src.workspace.postgres_store import PostgresWorkspaceStore

        self.store = PostgresWorkspaceStore(DSN, schema=SCHEMA, min_size=1, max_size=4)
        # Each test runs in its own tenant, so runs never collide.
        self.tenant = str(uuid.uuid4())
        self.store.ensure_organization(self.tenant, "Test organization", actor_id="test")

    def tearDown(self):
        self.store.close()

    def manuscript(self):
        row, _created = self.store.upsert_manuscript(
            self.tenant, title="T", original_filename="t.pdf",
            content_sha256="a" * 64, pdf_path="/tmp/t.pdf", actor_id="test",
        )
        return row

    def queued_job(self, **kwargs):
        manuscript = self.manuscript()
        return self.store.create_audit_job(
            self.tenant, manuscript["id"], {"pdf_path": "/tmp/t.pdf"}, "test", **kwargs
        )

    def test_skip_locked_lease_gives_one_winner(self):
        _audit, job = self.queued_job()
        first = self.store.lease_next_job("worker-1")
        self.assertEqual(str(first["id"]), str(job["id"]))
        self.assertIsNone(self.store.lease_next_job("worker-2"))

    def test_priority_and_backoff_order_the_queue(self):
        _low_audit, low = self.queued_job(priority=500)
        _high_audit, high = self.queued_job(priority=1)
        leased = self.store.lease_next_job("worker-1")
        self.assertEqual(str(leased["id"]), str(high["id"]))
        self.store.release_job(
            self.tenant, str(high["id"]), "worker-1", error_code="timeout", backoff_seconds=120
        )
        following = self.store.lease_next_job("worker-2")
        self.assertEqual(str(following["id"]), str(low["id"]),
                         "a job inside its backoff window must be skipped")

    def test_expired_leases_are_reclaimed_then_dead_lettered(self):
        _audit, job = self.queued_job(max_attempts=1)
        self.store.lease_next_job("worker-1", lease_seconds=5)
        time.sleep(6)
        self.assertGreaterEqual(self.store.reclaim_expired_leases(), 1)
        self.assertEqual(self.store.get_job(self.tenant, str(job["id"]))["state"], "dead_letter")

    def test_the_activity_trail_is_append_only_at_the_database_level(self):
        self.manuscript()
        with self.store._cursor() as cursor:  # noqa: SLF001 - deliberately testing the guard
            with self.assertRaises(Exception):
                cursor.execute(
                    "UPDATE activity SET action='tampered' WHERE tenant_id=%s", (self.tenant,)
                )

    def test_the_hash_chain_verifies_across_backends(self):
        self.manuscript()
        self.store.upsert_member(
            self.tenant, "r@example.org", display_name=None, email=None,
            roles=["reviewer"], actor_id="test",
        )
        verification = self.store.verify_activity_chain(self.tenant)
        self.assertTrue(verification["valid"])
        self.assertGreaterEqual(verification["event_count"], 3)

    def test_cross_tenant_reads_return_not_found(self):
        from src.workspace.common import WorkspaceNotFound

        audit, _job = self.queued_job()
        with self.assertRaises(WorkspaceNotFound):
            self.store.get_audit(str(uuid.uuid4()), str(audit["id"]))

    def test_decisions_are_idempotent_and_optimistically_concurrent(self):
        from src.workspace.common import WorkspaceConflict

        audit, _job = self.queued_job()
        audit_id = str(audit["id"])
        first = self.store.record_decision(
            self.tenant, audit_id, "finding-1", "confirm", "Checked the evidence.",
            "reviewer-a", "key-1",
        )
        replay = self.store.record_decision(
            self.tenant, audit_id, "finding-1", "confirm", "Checked the evidence.",
            "reviewer-a", "key-1",
        )
        self.assertEqual(str(first["id"]), str(replay["id"]))
        with self.assertRaises(WorkspaceConflict):
            self.store.record_decision(
                self.tenant, audit_id, "finding-1", "dismiss", "A stale edit.",
                "reviewer-b", "key-2",
            )
        superseding = self.store.record_decision(
            self.tenant, audit_id, "finding-1", "dismiss", "Second review.",
            "reviewer-b", "key-3", expected_current_id=str(first["id"]),
        )
        self.assertEqual(str(superseding["supersedes_decision_id"]), str(first["id"]))

    def test_telemetry_and_latency_windows_are_tenant_scoped(self):
        audit, job = self.queued_job()
        self.store.lease_next_job("worker-1")
        self.store.finish_job(
            self.tenant, str(job["id"]), success=True,
            summary={"total_references": 5, "resolved_references": 5},
        )
        self.store.record_telemetry(self.tenant, [
            {"stage": "parse", "duration_ms": 100, "cost_usd": 0.01, "audit_id": str(audit["id"])}
        ])
        self.assertEqual(len(self.store.telemetry_window(self.tenant, hours=1)), 1)
        self.assertEqual(len(self.store.telemetry_window(str(uuid.uuid4()), hours=1)), 0)
        self.assertEqual(len(self.store.audit_latency_window(self.tenant, hours=1)), 1)


if __name__ == "__main__":
    unittest.main()
