"""HTTP contract tests use injected offline services and never call providers."""

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

TENANT = "00000000-0000-4000-8000-000000000001"


def evidence_artifact():
    return {
        "schema_version": SCHEMA_VERSION,
        "manuscript_metadata": {"title": "API contract paper"},
        "citations_in_text": [],
        "scored_references": [
            {
                "reference_uid": "api-reference",
                "ref_id": "[1]",
                "original_data": {
                    "raw_text": "[1] A source",
                    "parsed": {"title": "A source"},
                    "extraction_status": "located",
                    "extraction_issues": [],
                    "source_locations": [{"page": 2}],
                },
                "resolution": {"status": "not_found", "metadata_differences": []},
                "external_metadata": {},
                "citation_count": 0,
                "evidence_status": "not_assessed",
                "claim_assessment": "not_assessed",
                "claim_assessments": [],
            }
        ],
    }


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = WorkspaceStore(self.root / "workspace.db")
        self.service = WorkspaceService(self.store, data_root=self.root / "data")
        self.client = TestClient(create_app(service=self.service, runner=None))

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
        output.parent.mkdir(parents=True)
        output.write_text(json.dumps(evidence_artifact()), encoding="utf-8")
        self.store.finish_job(
            TENANT,
            job.id,
            success=True,
            artifact_path=str(output),
            summary=build_review(evidence_artifact())["counts"],
        )
        return manuscript, audit

    def test_openapi_health_and_upload_contract(self):
        self.assertEqual(self.client.get("/api/v1/health").json()["api_version"], "v1")
        upload = self.client.post(
            "/api/v1/manuscripts",
            files={"file": ("paper.pdf", b"%PDF-1.7\ncontract\n%%EOF\n", "application/pdf")},
        )
        self.assertEqual(upload.status_code, 201)
        identifier = upload.json()["id"]
        listed = self.client.get("/api/v1/manuscripts").json()["items"]
        self.assertEqual([item["id"] for item in listed], [identifier])
        audit = self.client.post(
            f"/api/v1/manuscripts/{identifier}/audits",
            json={
                "force_parse": False,
                "force_enrich": False,
                "force_score": False,
                "run_missing_citations": False,
            },
        )
        self.assertEqual(audit.status_code, 202)
        self.assertEqual(audit.json()["job"]["state"], "queued")
        schema = self.client.get("/api/openapi.json").json()
        self.assertIn("/api/v1/review-cases", schema["paths"])

    def test_review_reference_decision_and_tenant_isolation(self):
        _manuscript, audit = self.completed_audit()
        review = self.client.get(f"/api/v1/audits/{audit.id}/review")
        self.assertEqual(review.status_code, 200)
        finding = review.json()["findings"][0]
        reference = self.client.get(f"/api/v1/audits/{audit.id}/references/api-reference")
        self.assertEqual(reference.json()["as_cited"]["raw_text"], "[1] A source")
        decision = self.client.post(
            f"/api/v1/audits/{audit.id}/findings/{finding['id']}/decisions",
            headers={"Idempotency-Key": "api-decision-0001"},
            json={
                "disposition": "needs_more_evidence",
                "rationale": "The cited source was not resolved.",
                "expected_current_decision_id": None,
            },
        )
        self.assertEqual(decision.status_code, 201)
        self.assertEqual(
            self.client.get(f"/api/v1/audits/{audit.id}/review").json()["counts"][
                "reviewer_decisions"
            ],
            1,
        )
        hidden = self.client.get(
            f"/api/v1/audits/{audit.id}",
            headers={"X-CitePrism-Tenant": "00000000-0000-4000-8000-000000000002"},
        )
        self.assertEqual(hidden.status_code, 404)
        self.assertTrue(self.client.get("/api/v1/activity/verification").json()["valid"])


if __name__ == "__main__":
    unittest.main()
