"""HTTP contract tests for the Phase 4 surface, using injected offline services."""

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from src.evidence import SCHEMA_VERSION
from src.evidence.review import build_review
from src.webapi.app import create_app
from src.workspace.models import AuditOptions
from src.workspace.service import WorkspaceService
from src.workspace.store import WorkspaceStore

from tests.workspace_fixtures import OTHER_TENANT, TENANT, evidence_artifact  # noqa: E402


class Phase4ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = WorkspaceStore(self.root / "workspace.db")
        self.service = WorkspaceService(self.store, data_root=self.root / "data")
        self.client = TestClient(create_app(service=self.service, runner=None))
        self.manuscript, self.audit = self.completed_audit()

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def completed_audit(self):
        manuscript, _ = self.service.register_bytes(
            TENANT, b"%PDF-1.7\nAPI test\n%%EOF\n", original_filename="api.pdf", actor_id="tester"
        )
        audit, job = self.service.create_audit(TENANT, manuscript.id, AuditOptions(), "tester")
        self.store.claim_job(TENANT, job.id, "test-worker")
        output = self.root / "data" / "scored" / "api.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence_artifact()), encoding="utf-8")
        self.store.finish_job(
            TENANT, job.id, success=True, artifact_path=str(output),
            summary=build_review(evidence_artifact())["counts"],
        )
        return manuscript, audit

    def as_role(self, roles: str):
        return {"X-CitePrism-Roles": roles, "X-CitePrism-Actor": "test-user"}

    # -- session and roles ---------------------------------------------
    def test_session_reports_permissions_and_runtime_features(self):
        body = self.client.get("/api/v1/session").json()
        self.assertEqual(body["tenant_id"], TENANT)
        self.assertIn("review.decide", body["permissions"])
        self.assertIn(body["workspace_backend"], {"sqlite", "postgres"})
        self.assertIn("resolution_providers", body["features"])

    def test_role_catalogue_is_published(self):
        roles = {item["role"] for item in self.client.get("/api/v1/organizations/roles").json()["items"]}
        self.assertIn("lead_reviewer", roles)
        self.assertIn("viewer", roles)

    def test_a_viewer_cannot_write(self):
        response = self.client.put(
            "/api/v1/organizations/current", json={"name": "New name"},
            headers=self.as_role("viewer"),
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("organization.write", response.json()["detail"])

    # -- organizations and assignment ----------------------------------
    def test_membership_and_assignment_round_trip(self):
        member = self.client.put(
            "/api/v1/organizations/members",
            json={"subject": "reviewer@example.org", "roles": ["reviewer"],
                  "display_name": "A Reviewer", "email": "reviewer@example.org"},
        )
        self.assertEqual(member.status_code, 200)
        self.assertEqual(member.json()["roles"], ["reviewer"])

        review = self.client.get(f"/api/v1/audits/{self.audit.id}/review").json()
        finding_id = review["findings"][0]["id"]

        assigned = self.client.post(
            f"/api/v1/audits/{self.audit.id}/assignments",
            json={"finding_id": finding_id, "assignee": "reviewer@example.org",
                  "note": "Please check the provider log", "due_at": None},
        )
        self.assertEqual(assigned.status_code, 201)

        workload = {item["subject"]: item["open_cases"]
                    for item in self.client.get("/api/v1/assignments/workload").json()["items"]}
        self.assertEqual(workload["reviewer@example.org"], 1)

        released = self.client.delete(
            f"/api/v1/audits/{self.audit.id}/assignments/{finding_id}"
        )
        self.assertEqual(released.status_code, 200)
        self.assertEqual(released.json()["state"], "released")

    def test_unrecognized_roles_are_refused(self):
        response = self.client.put(
            "/api/v1/organizations/members",
            json={"subject": "x@example.org", "roles": ["wizard"],
                  "display_name": None, "email": None},
        )
        self.assertEqual(response.status_code, 422)

    # -- repairs --------------------------------------------------------
    def test_repair_plan_offers_the_targeted_action(self):
        body = self.client.get(f"/api/v1/audits/{self.audit.id}/repairs").json()
        self.assertGreaterEqual(body["system_issue_count"], 1)
        categories = {plan["issue_category"] for plan in body["plans"]}
        self.assertIn("assessment_error", categories)
        plan = next(p for p in body["plans"] if p["issue_category"] == "assessment_error")
        self.assertEqual(plan["action"], "rerun_assessment")
        self.assertFalse(plan["options"]["force_parse"])

    def test_running_a_repair_creates_a_new_audit_and_preserves_the_original(self):
        response = self.client.post(
            f"/api/v1/audits/{self.audit.id}/repairs",
            headers={"Idempotency-Key": "repair-key-0001"},
            json={"categories": ["assessment_error"]},
        )
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["repaired_categories"], ["assessment_error"])
        self.assertNotEqual(body["audit"]["id"], self.audit.id)

        original = self.client.get(f"/api/v1/audits/{self.audit.id}").json()
        self.assertEqual(original["state"], "completed_with_issues")
        self.assertIsNotNone(original["artifact_path"])

        history = self.client.get(f"/api/v1/audits/{self.audit.id}/repairs").json()["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["state"], "queued")

    def test_repairing_an_absent_category_is_rejected(self):
        response = self.client.post(
            f"/api/v1/audits/{self.audit.id}/repairs",
            headers={"Idempotency-Key": "repair-key-0002"},
            json={"categories": ["bibliography_review"]},
        )
        self.assertEqual(response.status_code, 422)

    def test_a_viewer_cannot_run_a_repair(self):
        response = self.client.post(
            f"/api/v1/audits/{self.audit.id}/repairs",
            headers={"Idempotency-Key": "repair-key-0003", **self.as_role("viewer")},
            json={"categories": ["assessment_error"]},
        )
        self.assertEqual(response.status_code, 403)

    # -- exports ---------------------------------------------------------
    def test_every_export_format_is_produced_signed_and_downloadable(self):
        for export_format, prefix in (
            ("json", b"{"), ("html", b"<!doctype"), ("pdf", b"%PDF"), ("bundle", b"PK"),
        ):
            created = self.client.post(
                f"/api/v1/audits/{self.audit.id}/exports", json={"format": export_format}
            )
            self.assertEqual(created.status_code, 201, export_format)
            record = created.json()
            self.assertEqual(record["format"], export_format)
            self.assertEqual(len(record["content_sha256"]), 64)
            self.assertTrue(record["signature"])

            download = self.client.get(f"/api/v1/exports/{record['id']}/download")
            self.assertEqual(download.status_code, 200, export_format)
            self.assertTrue(download.content.startswith(prefix), export_format)
            self.assertEqual(
                download.headers["X-CitePrism-Content-SHA256"], record["content_sha256"]
            )

    def test_the_export_manifest_lists_every_file_with_its_digest(self):
        record = self.client.post(
            f"/api/v1/audits/{self.audit.id}/exports", json={"format": "bundle"}
        ).json()
        manifest = self.client.get(f"/api/v1/exports/{record['id']}/manifest").json()["manifest"]
        names = {item["name"] for item in manifest["files"]}
        self.assertEqual(names, {"review.json", "review.html", "review.pdf", "report.pdf"})
        self.assertTrue(all(len(item["sha256"]) == 64 for item in manifest["files"]))
        self.assertIn("signature", manifest)
        self.assertTrue(manifest["limitations"])

    def test_exports_are_tenant_scoped(self):
        record = self.client.post(
            f"/api/v1/audits/{self.audit.id}/exports", json={"format": "json"}
        ).json()
        hidden = self.client.get(
            f"/api/v1/exports/{record['id']}/download",
            headers={"X-CitePrism-Tenant": OTHER_TENANT},
        )
        self.assertEqual(hidden.status_code, 404)

    # -- insights ---------------------------------------------------------
    def test_insight_endpoints_carry_their_caveats(self):
        overlap = self.client.get(f"/api/v1/audits/{self.audit.id}/author-overlap").json()
        self.assertEqual(overlap["counts"]["total_references"], 1)
        self.assertIn("not_a_claim", overlap)

        leads = self.client.get(f"/api/v1/audits/{self.audit.id}/missing-citations").json()
        self.assertEqual(len(leads["items"]), 1)
        self.assertEqual(leads["items"][0]["verification_status"], "unverified_model_suggestion")

    def test_assistant_summarises_and_answers_from_the_audit(self):
        summary = self.client.get(f"/api/v1/audits/{self.audit.id}/assistant").json()
        self.assertEqual(summary["intent"], "overview")
        self.assertIn("1 reference", summary["text"])
        self.assertEqual(summary["grounding"], "audit_results")
        self.assertTrue(summary["suggestions"])

        answer = self.client.post(f"/api/v1/audits/{self.audit.id}/assistant",
                                  json={"question": "Explain reference 1"}).json()
        self.assertEqual(answer["intent"], "reference")
        self.assertEqual(answer["references"][0]["marker"], "[1]")
        self.assertIn("Check did not finish", " ".join(answer["points"]))

    def test_a_book_is_detected_and_split_into_chapters(self):
        volume = Path(__file__).parent / "fixtures" / "evidence" / "edited_volume.pdf"
        book, _ = self.service.register_bytes(TENANT, volume.read_bytes(), original_filename="volume.pdf",
                                              actor_id="tester", title="Lehren und Lernen")
        structure = self.client.get(f"/api/v1/manuscripts/{book.id}/structure").json()
        self.assertTrue(structure["multi_part"])
        self.assertEqual([part["references"] for part in structure["parts"]], [5, 7])

        created = self.client.post(f"/api/v1/manuscripts/{book.id}/parts", json={"parts": [2]})
        self.assertEqual(created.status_code, 201)
        items = created.json()["items"]
        self.assertEqual(len(items), 1)
        child = items[0]["manuscript"]
        self.assertTrue(child["title"].startswith("2. Feedback that works"))
        self.assertTrue(child["title"].endswith("— Lehren und Lernen"))
        self.assertEqual(child["original_filename"], "volume_ch02_p5-6.pdf")   # never the stored hash
        titles = [item["title"] for item in self.client.get("/api/v1/manuscripts").json()["items"]]
        self.assertIn("Lehren und Lernen", titles)          # the original is kept

    def test_a_single_paper_cannot_be_split(self):
        response = self.client.post(f"/api/v1/manuscripts/{self.manuscript.id}/parts", json={})
        self.assertIn(response.status_code, {404, 422})

    def test_assistant_rejects_oversized_questions(self):
        # Questions may be up to 1,000 characters (long, vague messages in everyday words).
        response = self.client.post(f"/api/v1/audits/{self.audit.id}/assistant", json={"question": "x" * 1001})
        self.assertEqual(response.status_code, 422)

    def test_assistant_is_tenant_scoped(self):
        response = self.client.get(f"/api/v1/audits/{self.audit.id}/assistant",
                                   headers={"X-CitePrism-Tenant": OTHER_TENANT})
        self.assertIn(response.status_code, {403, 404})

    # -- telemetry and evaluation ------------------------------------------
    def test_slo_endpoint_returns_every_objective(self):
        body = self.client.get("/api/v1/telemetry/slos").json()
        keys = {item["key"] for item in body["objectives"]}
        self.assertIn("audit_latency_p95", keys)
        self.assertIn("cost_per_audit", keys)
        self.assertIn("system_issue_ratio", keys)
        self.assertIn("queue", body["totals"])

    def test_queue_endpoint_reports_depth(self):
        body = self.client.get("/api/v1/queue").json()
        self.assertIn("queued", body)
        self.assertIn("dead_letter", body)

    def test_evaluation_dataset_listing_is_available(self):
        body = self.client.get("/api/v1/evaluation/datasets").json()
        self.assertIn("items", body)
        self.assertIn("not establish", body["note"])

    # -- contract ------------------------------------------------------------
    def test_openapi_publishes_the_phase4_surface(self):
        paths = self.client.get("/api/openapi.json").json()["paths"]
        for path in (
            "/api/v1/session",
            "/api/v1/organizations/members",
            "/api/v1/assignments/workload",
            "/api/v1/audits/{audit_id}/repairs",
            "/api/v1/audits/{audit_id}/exports",
            "/api/v1/audits/{audit_id}/author-overlap",
            "/api/v1/audits/{audit_id}/assistant",
            "/api/v1/telemetry/slos",
        ):
            self.assertIn(path, paths, path)

    def test_phase3_endpoints_are_unchanged(self):
        self.assertEqual(self.client.get("/api/v1/health").json()["api_version"], "v1")
        self.assertEqual(
            self.client.get(f"/api/v1/audits/{self.audit.id}/review").status_code, 200
        )
        self.assertTrue(self.client.get("/api/v1/activity/verification").json()["valid"])
        hidden = self.client.get(
            f"/api/v1/audits/{self.audit.id}", headers={"X-CitePrism-Tenant": OTHER_TENANT}
        )
        self.assertEqual(hidden.status_code, 404)


if __name__ == "__main__":
    unittest.main()
