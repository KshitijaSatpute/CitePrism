"""Hydrator output must match the strict contracts.

Every workspace contract sets `extra="forbid"`. That is deliberate — an
unexpected key should be caught rather than silently carried into an API
response. The cost is that adding a database column and surfacing it through a
hydrator without declaring it on the model turns every write into a validation
error at runtime.

This suite closes that gap: it walks each hydrator with a representative row and
asserts the produced keys are all declared fields. It is a schema-drift alarm,
not a behaviour test.
"""

import unittest
from pathlib import Path

from src.workspace import models
from src.workspace.store import (
    hydrate_activity,
    hydrate_audit,
    hydrate_correction,
    hydrate_decision,
    hydrate_job,
    hydrate_manuscript,
)

MANUSCRIPT_ROW = {
    "id": "01a08b3c-0000-4000-8000-000000000001",
    "title": "A traceable paper",
    "original_filename": "paper.pdf",
    "content_sha256": "a" * 64,
    "pdf_path": "/data/workspace_uploads/aaa.pdf",
    "object_backend": "local",
    "object_key": "tenant/manuscripts/sha256/aa/aa/" + "a" * 64 + ".pdf",
    "object_version_id": None,
    "text_layer_status": "ocr_applied",
    "legacy_document_id": 7,
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}

AUDIT_ROW = {
    "id": "01a08b3c-0000-4000-8000-000000000002",
    "manuscript_id": MANUSCRIPT_ROW["id"],
    "job_id": "01a08b3c-0000-4000-8000-000000000003",
    "state": "completed_with_issues",
    "artifact_path": "/data/scored/artifact.json",
    "artifact_key": "tenant/artifacts/sha256/bb/bb/" + "b" * 64 + ".json",
    "summary_json": '{"total_references":20}',
    "created_at": "2026-01-01T00:00:00Z",
    "started_at": "2026-01-01T00:01:00Z",
    "completed_at": "2026-01-01T00:09:00Z",
}

JOB_ROW = {
    "id": AUDIT_ROW["job_id"],
    "audit_id": AUDIT_ROW["id"],
    "state": "succeeded",
    "progress": 100,
    "stage": "completed",
    "attempts": 1,
    "max_attempts": 3,
    "kind": "audit",
    "priority": 100,
    "available_at": "2026-01-01T00:00:00Z",
    "cancel_requested": 0,
    "created_at": "2026-01-01T00:00:00Z",
}

DECISION_ROW = {
    "id": "01a08b3c-0000-4000-8000-000000000004",
    "audit_id": AUDIT_ROW["id"],
    "finding_id": "finding-1",
    "disposition": "confirm",
    "rationale": "Checked against the publisher record.",
    "reviewer_id": "reviewer-a",
    "supersedes_decision_id": None,
    "created_at": "2026-01-01T00:10:00Z",
}

CORRECTION_ROW = {
    "id": "01a08b3c-0000-4000-8000-000000000005",
    "audit_id": AUDIT_ROW["id"],
    "kind": "metadata",
    "reference_ids_json": '["ref-1"]',
    "proposed_value_json": '{"title":"Corrected"}',
    "rationale": "Compared with the publisher record.",
    "reviewer_id": "reviewer-a",
    "state": "proposed",
    "supersedes_correction_id": None,
    "created_at": "2026-01-01T00:11:00Z",
}

ACTIVITY_ROW = {
    "id": 1,
    "audit_id": AUDIT_ROW["id"],
    "actor_id": "reviewer-a",
    "action": "decision.recorded",
    "entity_type": "finding",
    "entity_id": "finding-1",
    "payload_json": "{}",
    "previous_hash": "0" * 64,
    "event_hash": "1" * 64,
    "created_at": "2026-01-01T00:10:00Z",
}


def declared_fields(model) -> set[str]:
    names: dict = {}
    for klass in reversed(model.__mro__):
        names.update(getattr(klass, "__annotations__", {}) or {})
    return set(names) - {"model_config"}


class ContractAlignmentTests(unittest.TestCase):
    def assert_aligned(self, model, payload, label):
        extra = set(payload) - declared_fields(model)
        self.assertFalse(
            extra,
            f"{label} emits undeclared field(s) {sorted(extra)}. Add them to the "
            f"{model.__name__} contract or stop hydrating them.",
        )

    def test_manuscript_hydrator_matches_its_contract(self):
        self.assert_aligned(
            models.Manuscript, hydrate_manuscript(MANUSCRIPT_ROW), "hydrate_manuscript"
        )

    def test_audit_hydrator_matches_its_contract(self):
        self.assert_aligned(models.Audit, hydrate_audit(AUDIT_ROW), "hydrate_audit")

    def test_job_hydrator_matches_its_contract(self):
        self.assert_aligned(models.Job, hydrate_job(JOB_ROW), "hydrate_job")

    def test_decision_hydrator_matches_its_contract(self):
        self.assert_aligned(models.Decision, hydrate_decision(DECISION_ROW), "hydrate_decision")

    def test_correction_hydrator_matches_its_contract(self):
        self.assert_aligned(
            models.Correction, hydrate_correction(CORRECTION_ROW), "hydrate_correction"
        )

    def test_activity_hydrator_matches_its_contract(self):
        self.assert_aligned(
            models.Activity, hydrate_activity([ACTIVITY_ROW])[0], "hydrate_activity"
        )

    def test_a_phase3_row_without_phase4_columns_still_validates(self):
        """An existing SQLite database predates the Phase 4 columns."""
        legacy = {
            key: value
            for key, value in MANUSCRIPT_ROW.items()
            if key not in {"object_backend", "object_key", "object_version_id",
                           "text_layer_status"}
        }
        payload = hydrate_manuscript(legacy)
        self.assertEqual(payload["object_backend"], "local")
        self.assertEqual(payload["text_layer_status"], "unknown")
        self.assertIsNone(payload["object_key"])
        self.assert_aligned(models.Manuscript, payload, "hydrate_manuscript (legacy row)")


class RoleSubsumptionTests(unittest.TestCase):
    """`can(<role>)` asks about authority, not about a role name string."""

    def principal(self, roles):
        from src.webapi.auth import Principal
        from src.workspace.rbac import normalize_roles

        return Principal("00000000-0000-4000-8000-000000000001", "actor",
                         normalize_roles(roles))

    def test_a_senior_role_satisfies_a_junior_role_question(self):
        self.assertTrue(self.principal("org_admin").can("reviewer"))
        self.assertTrue(self.principal("org_admin").can("operator"))
        self.assertTrue(self.principal("lead_reviewer").can("reviewer"))
        self.assertTrue(self.principal("administrator").can("reviewer"))

    def test_a_junior_role_does_not_satisfy_a_senior_one(self):
        self.assertFalse(self.principal("viewer").can("reviewer"))
        self.assertFalse(self.principal("reviewer").can("operator"))
        self.assertFalse(self.principal("reviewer").can("org_admin"))

    def test_permission_questions_still_work(self):
        self.assertTrue(self.principal("org_admin").can("review.decide"))
        self.assertFalse(self.principal("viewer").can("review.decide"))

    def test_an_unknown_role_name_is_not_granted(self):
        self.assertFalse(self.principal("reviewer").can("wizard"))


class RouteAnnotationTests(unittest.TestCase):
    """Guard the PEP 563 trap that silently turned dependencies into query params.

    Phase 4 routes live inside `register_phase4_routes`, and the `Principal`
    dependency alias is a closure local. If `from __future__ import annotations`
    is ever added to that module, FastAPI resolves the annotation string against
    module globals, fails to find the alias, and demotes `user` to a required
    query parameter. Every route then answers HTTP 422 and OpenAPI generation
    raises. The symptom is far away from the cause, so it is asserted here.
    """

    def test_the_routes_module_does_not_use_postponed_annotations(self):
        # Parse rather than import: this must hold where FastAPI is absent.
        # Parse rather than grep: the module docstring explains the trap, and a
        # text search would match that explanation instead of a real import.
        import ast

        source = (
            Path(__file__).resolve().parents[1] / "src" / "webapi" / "routes_phase4.py"
        ).read_text(encoding="utf-8")
        postponed = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
            and any(alias.name == "annotations" for alias in node.names)
        ]
        self.assertFalse(
            postponed,
            "routes_phase4 defines routes in a closure; postponed annotations "
            "break FastAPI dependency resolution for the Principal alias and "
            "silently demote it to a required query parameter.",
        )

    def test_no_route_exposes_user_as_a_query_parameter(self):
        try:
            from fastapi.testclient import TestClient  # noqa: F401
        except ImportError:  # pragma: no cover - offline environment
            self.skipTest("fastapi is not installed")
        import tempfile

        from src.webapi.app import create_app
        from src.workspace.service import WorkspaceService
        from src.workspace.store import WorkspaceStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = WorkspaceService(
                WorkspaceStore(root / "workspace.db"), data_root=root / "data"
            )
            app = create_app(service=service, runner=None)
            schema = app.openapi()

        offenders = []
        for path, operations in schema["paths"].items():
            for method, operation in operations.items():
                for parameter in operation.get("parameters", []):
                    if parameter.get("name") == "user":
                        offenders.append(f"{method.upper()} {path}")
        self.assertFalse(
            offenders,
            "these routes expose the authenticated principal as a request "
            f"parameter instead of a dependency: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
