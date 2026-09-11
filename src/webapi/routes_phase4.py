"""Phase 4 HTTP surface: organizations, assignment, repairs, exports, insights,
telemetry and evaluation.

These routes are registered onto the existing app rather than replacing it, so
every Phase 3 contract keeps working unchanged. Authorization is uniform: each
route names the permission it needs and `require` answers from the RBAC matrix.

`from __future__ import annotations` must NOT be added to this module. Routes are
defined inside `register_phase4_routes`, and the `Principal` dependency alias is
a closure local. PEP 563 would turn every annotation into a string that FastAPI
resolves against module globals, where that alias does not exist — so `user`
would silently degrade into a required query parameter and every route would
answer HTTP 422.
"""

import logging
from typing import Annotated, Any, Literal

from fastapi import Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from src.webapi.insights import author_overlap, missing_citations
from src.workspace.rbac import AuthorizationError, describe_roles

logger = logging.getLogger(__name__)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class MemberUpsert(Contract):
    subject: str = Field(min_length=1, max_length=255)
    roles: list[str] = Field(min_length=1, max_length=10)
    display_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)


class AssignmentCreate(Contract):
    finding_id: str = Field(min_length=1, max_length=200)
    assignee: str = Field(min_length=1, max_length=255)
    note: str | None = Field(default=None, max_length=2000)
    due_at: str | None = Field(default=None, max_length=40)


class RepairRequest(Contract):
    categories: list[str] = Field(min_length=1, max_length=5)


class ExportRequest(Contract):
    format: Literal["pdf", "html", "json", "bundle"] = "bundle"


class OrganizationUpdate(Contract):
    name: str = Field(min_length=1, max_length=200)


MEDIA_TYPES = {
    "json": "application/json",
    "html": "text/html; charset=utf-8",
    "pdf": "application/pdf",
    "bundle": "application/zip",
}


def register_phase4_routes(app, *, principal, service, store, config, runtime: dict) -> None:
    """Attach every Phase 4 route to an existing FastAPI application."""

    def authorize(user, permission: str) -> None:
        try:
            user.require(permission)
        except AuthorizationError as error:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=str(error)
            ) from error

    def export_service():
        value = runtime.get("export_service")
        if value is None:
            raise HTTPException(status_code=503, detail="the export service is not configured")
        return value

    def repair_service():
        value = runtime.get("repair_service")
        if value is None:
            raise HTTPException(status_code=503, detail="the repair service is not configured")
        return value

    Principal = Annotated[Any, Depends(principal)]

    # ------------------------------------------------------------------
    # identity and organizations
    # ------------------------------------------------------------------
    @app.get("/api/v1/session", tags=["identity"])
    def session(user: Principal):
        """What this caller may do. The client uses it to hide unusable controls."""
        return {
            **user.as_dict(),
            "workspace_backend": getattr(config, "WORKSPACE_BACKEND", "sqlite"),
            "object_backend": getattr(config, "WORKSPACE_OBJECT_BACKEND", "local"),
            "features": {
                "ocr": bool(getattr(config, "ENABLE_OCR", False)),
                "full_text": bool(getattr(config, "ENABLE_FULL_TEXT_ACQUISITION", False)),
                "resolution_providers": list(getattr(config, "RESOLUTION_PROVIDERS", ())),
                "telemetry": bool(getattr(config, "ENABLE_TELEMETRY", True)),
                "export_signing": getattr(config, "EXPORT_SIGNING_ALGORITHM", "none"),
            },
        }

    @app.get("/api/v1/organizations/roles", tags=["identity"])
    def roles():
        return {"items": describe_roles()}

    @app.get("/api/v1/organizations/current", tags=["identity"])
    def current_organization(user: Principal):
        authorize(user, "organization.read")
        organization = store.get_organization(user.tenant_id) or store.ensure_organization(
            user.tenant_id, "Organization", actor_id=user.actor_id
        )
        return organization

    @app.put("/api/v1/organizations/current", tags=["identity"])
    def rename_organization(value: OrganizationUpdate, user: Principal):
        authorize(user, "organization.write")
        return store.ensure_organization(user.tenant_id, value.name, actor_id=user.actor_id)

    @app.get("/api/v1/organizations/members", tags=["identity"])
    def list_members(user: Principal):
        authorize(user, "organization.read")
        return {"items": store.list_members(user.tenant_id)}

    @app.put("/api/v1/organizations/members", status_code=200, tags=["identity"])
    def upsert_member(value: MemberUpsert, user: Principal):
        authorize(user, "organization.write")
        from src.workspace.rbac import normalize_roles

        resolved = normalize_roles(value.roles)
        if not resolved:
            raise HTTPException(
                status_code=422,
                detail="none of the supplied roles are recognized; see /api/v1/organizations/roles",
            )
        return store.upsert_member(
            user.tenant_id,
            value.subject,
            display_name=value.display_name,
            email=value.email,
            roles=sorted(resolved),
            actor_id=user.actor_id,
        )

    @app.delete("/api/v1/organizations/members/{subject}", tags=["identity"])
    def deactivate_member(subject: str, user: Principal):
        authorize(user, "organization.write")
        if subject == user.actor_id:
            raise HTTPException(
                status_code=409, detail="an administrator cannot deactivate their own account"
            )
        return store.deactivate_member(user.tenant_id, subject, user.actor_id)

    # ------------------------------------------------------------------
    # reviewer assignment
    # ------------------------------------------------------------------
    @app.get("/api/v1/assignments", tags=["assignment"])
    def list_assignments(
        user: Principal,
        audit_id: str | None = None,
        assignee: str | None = None,
        mine: bool = False,
    ):
        authorize(user, "assignment.read")
        return {
            "items": store.list_assignments(
                user.tenant_id,
                audit_id=audit_id,
                assignee=user.actor_id if mine else assignee,
            )
        }

    @app.get("/api/v1/assignments/workload", tags=["assignment"])
    def workload(user: Principal):
        authorize(user, "assignment.read")
        return {"items": store.assignment_workload(user.tenant_id)}

    @app.post("/api/v1/audits/{audit_id}/assignments", status_code=201, tags=["assignment"])
    def assign(audit_id: str, value: AssignmentCreate, user: Principal):
        authorize(user, "assignment.write")
        return store.assign_case(
            user.tenant_id,
            audit_id,
            value.finding_id,
            value.assignee,
            actor_id=user.actor_id,
            note=value.note,
            due_at=value.due_at,
        )

    @app.delete("/api/v1/audits/{audit_id}/assignments/{finding_id}", tags=["assignment"])
    def unassign(audit_id: str, finding_id: str, user: Principal):
        authorize(user, "assignment.write")
        return store.unassign_case(user.tenant_id, audit_id, finding_id, user.actor_id)

    # ------------------------------------------------------------------
    # automatic repair
    # ------------------------------------------------------------------
    @app.get("/api/v1/audits/{audit_id}/repairs", tags=["repair"])
    def repair_plan(audit_id: str, user: Principal):
        authorize(user, "review.read")
        return repair_service().plan(user.tenant_id, audit_id)

    @app.post("/api/v1/audits/{audit_id}/repairs", status_code=202, tags=["repair"])
    def run_repair(
        audit_id: str,
        value: RepairRequest,
        user: Principal,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
        ],
    ):
        authorize(user, "repair.run")
        try:
            return repair_service().execute(
                user.tenant_id,
                audit_id,
                value.categories,
                actor_id=user.actor_id,
                idempotency_key=idempotency_key,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    # ------------------------------------------------------------------
    # signed review packages
    # ------------------------------------------------------------------
    @app.get("/api/v1/audits/{audit_id}/exports", tags=["export"])
    def list_exports(audit_id: str, user: Principal):
        authorize(user, "export.read")
        return {"items": store.list_exports(user.tenant_id, audit_id)}

    @app.post("/api/v1/audits/{audit_id}/exports", status_code=201, tags=["export"])
    def create_export(audit_id: str, value: ExportRequest, user: Principal):
        authorize(user, "export.create")
        try:
            return export_service().create(
                user.tenant_id, audit_id, value.format, actor_id=user.actor_id
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/exports/{export_id}/download", tags=["export"])
    def download_export(export_id: str, user: Principal):
        authorize(user, "export.read")
        record, payload = export_service().download(user.tenant_id, export_id)
        media_type = MEDIA_TYPES.get(record["format"], "application/octet-stream")
        suffix = {"json": "json", "html": "html", "pdf": "pdf", "bundle": "zip"}[record["format"]]
        filename = f"citeprism-review-{str(record['audit_id'])[:8]}.{suffix}"
        return Response(
            content=payload,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-CitePrism-Content-SHA256": record["content_sha256"],
                "X-CitePrism-Signature-Key-Id": record["signature_key_id"],
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/v1/exports/{export_id}/manifest", tags=["export"])
    def export_manifest(export_id: str, user: Principal):
        authorize(user, "export.read")
        record = store.get_export(user.tenant_id, export_id)
        return {
            "export_id": record["id"],
            "audit_id": record["audit_id"],
            "format": record["format"],
            "content_sha256": record["content_sha256"],
            "size_bytes": record["size_bytes"],
            "manifest": record["manifest"],
        }

    # ------------------------------------------------------------------
    # insight modules
    # ------------------------------------------------------------------
    @app.get("/api/v1/audits/{audit_id}/missing-citations", tags=["insight"])
    def audit_missing_citations(audit_id: str, user: Principal):
        authorize(user, "review.read")
        return missing_citations(service.load_artifact(user.tenant_id, audit_id))

    @app.get("/api/v1/audits/{audit_id}/author-overlap", tags=["insight"])
    def audit_author_overlap(audit_id: str, user: Principal):
        authorize(user, "review.read")
        return author_overlap(service.load_artifact(user.tenant_id, audit_id))

    # ------------------------------------------------------------------
    # telemetry and service objectives
    # ------------------------------------------------------------------
    @app.get("/api/v1/telemetry/slos", tags=["telemetry"])
    def slos(
        user: Principal,
        hours: int = Query(default=0, ge=0, le=8760),
    ):
        authorize(user, "telemetry.read")
        from src.telemetry.slo import evaluate_slos

        window = hours or int(getattr(config, "SLO_WINDOW_HOURS", 168))
        jobs = (
            store.audit_latency_window(user.tenant_id, hours=window)
            if hasattr(store, "audit_latency_window")
            else []
        )
        telemetry = (
            store.telemetry_window(user.tenant_id, hours=window)
            if hasattr(store, "telemetry_window")
            else []
        )
        queue = store.queue_depth(user.tenant_id) if hasattr(store, "queue_depth") else {}
        return evaluate_slos(config, jobs=jobs, telemetry=telemetry, queue=queue).as_dict()

    @app.get("/api/v1/telemetry/audits/{audit_id}", tags=["telemetry"])
    def audit_telemetry(audit_id: str, user: Principal):
        authorize(user, "telemetry.read")
        events = store.telemetry_window(user.tenant_id, hours=8760, audit_id=audit_id)
        return {
            "audit_id": audit_id,
            "events": events,
            "totals": {
                "cost_usd": round(sum(float(e.get("cost_usd") or 0) for e in events), 6),
                "duration_ms": sum(int(e.get("duration_ms") or 0) for e in events),
                "input_tokens": sum(int(e.get("input_tokens") or 0) for e in events),
                "output_tokens": sum(int(e.get("output_tokens") or 0) for e in events),
                "provider_requests": sum(int(e.get("requests") or 0) for e in events),
            },
        }

    @app.get("/api/v1/queue", tags=["telemetry"])
    def queue_state(user: Principal):
        authorize(user, "telemetry.read")
        return store.queue_depth(user.tenant_id)

    # ------------------------------------------------------------------
    # evaluation
    # ------------------------------------------------------------------
    @app.get("/api/v1/evaluation/datasets", tags=["evaluation"])
    def datasets(user: Principal):
        authorize(user, "evaluation.read")
        from pathlib import Path

        from src.evaluation.dataset import load_dataset

        directory = Path(getattr(config, "EVALUATION_DATASET_DIR", "data/evaluation"))
        if not directory.is_absolute():
            directory = Path(config.BASE_DIR) / directory
        items = []
        if directory.is_dir():
            for path in sorted(directory.glob("*.jsonl")):
                try:
                    dataset = load_dataset(path)
                    items.append({**dataset.summary(), "path": path.name,
                                  "problems": dataset.validate()[:5]})
                except Exception as error:  # noqa: BLE001
                    items.append({"name": path.stem, "path": path.name,
                                  "problems": [f"unreadable: {type(error).__name__}"]})
        return {
            "items": items,
            "directory": str(directory),
            "note": (
                "Metrics describe agreement with these annotators on these "
                "manuscripts. They do not establish scientific correctness."
            ),
        }

    @app.get("/api/v1/evaluation/audits/{audit_id}", tags=["evaluation"])
    def evaluate_one(
        audit_id: str,
        user: Principal,
        dataset: str = Query(min_length=1, max_length=120),
    ):
        authorize(user, "evaluation.read")
        from pathlib import Path

        from src.evaluation.dataset import load_dataset
        from src.evaluation.runner import evaluate_audit

        directory = Path(getattr(config, "EVALUATION_DATASET_DIR", "data/evaluation"))
        if not directory.is_absolute():
            directory = Path(config.BASE_DIR) / directory
        path = (directory / dataset).resolve()
        if not path.is_relative_to(directory.resolve()) or not path.is_file():
            raise HTTPException(status_code=404, detail="the dataset was not found")

        artifact = service.load_artifact(user.tenant_id, audit_id)
        digest = (artifact.get("source_document") or {}).get("pdf_sha256")
        loaded = load_dataset(path)
        items = loaded.by_manuscript(digest) if digest else []
        if not items:
            raise HTTPException(
                status_code=422,
                detail="this dataset contains no labels for the manuscript in this audit",
            )
        result = evaluate_audit(
            artifact, items, bins=int(getattr(config, "EVALUATION_CALIBRATION_BINS", 10))
        )
        result.pop("_pairs", None)
        return {"audit_id": audit_id, "dataset": loaded.summary(), **result}


__all__ = ["register_phase4_routes"]
