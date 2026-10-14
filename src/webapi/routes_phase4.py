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
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from src.assistant import AuditAssistant
from src.webapi.insights import author_overlap, missing_citations
from src.workspace.data_policy import data_handling
from src.workspace.exports import download_filename
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
    # "report" is the plain-language report with recommended steps (PDF only).
    variant: Literal["review", "report"] = "review"
    style: Literal["standard", "simple"] = "standard"


class SplitRequest(Contract):
    # None splits every chapter; an empty list is a mistake, not "everything".
    parts: list[Annotated[int, Field(ge=1, le=10000)]] | None = Field(default=None, min_length=1, max_length=200)


class AssistantTurn(Contract):
    """One earlier turn of the conversation, as the client shows it.

    Used only to resolve what a follow-up refers to ("why?", "the second one");
    never as a source of facts, which always come from the audit itself.
    """
    role: Literal["user", "assistant"]
    text: str = Field(default="", max_length=4000)
    intent: str | None = Field(default=None, max_length=40)
    reference_ids: list[Annotated[str, StringConstraints(max_length=128)]] = Field(default_factory=list,
                                                                                   max_length=40)
    offer: str | None = Field(default=None, max_length=40)
    style: Literal["simple", "standard"] | None = None  # the register of that answer
    # The topics of a several-part answer, so "explain it more simply" repeats them.
    topics: list[Annotated[str, StringConstraints(max_length=40)]] = Field(default_factory=list, max_length=8)


class AssistantQuestion(Contract):
    question: str = Field(min_length=1, max_length=1000)
    history: list[AssistantTurn] = Field(default_factory=list, max_length=12)


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
                "data_handling": data_handling(config),
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
                user.tenant_id, audit_id, value.format, actor_id=user.actor_id,
                variant=value.variant, style=value.style,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/exports/{export_id}/download", tags=["export"])
    def download_export(export_id: str, user: Principal):
        authorize(user, "export.read")
        record, payload = export_service().download(user.tenant_id, export_id)
        media_type = MEDIA_TYPES.get(record["format"], "application/octet-stream")
        filename = download_filename(record)
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
    # books and proceedings: one manuscript per chapter
    # ------------------------------------------------------------------
    def manuscript_pdf(user, manuscript_id: str):
        from pathlib import Path
        manuscript = service.get_manuscript(user.tenant_id, manuscript_id)
        path = Path(manuscript.pdf_path).resolve()
        if not path.is_relative_to(service.data_root) or not path.is_file():
            raise HTTPException(status_code=404, detail="the manuscript PDF is unavailable")
        return manuscript, path

    STRUCTURE_FAILURE_SECONDS = 600

    def structure_of(manuscript, path):
        """Chapters of a PDF, read once per file content.

        Successes are kept (a file's content never changes). Failures are kept for ten
        minutes: long enough that a PDF which took minutes to fail is not read again on
        every click, short enough that a timeout on a busy server is retried.
        """
        import threading
        import time
        cache = runtime.setdefault("structure_cache", {})          # sha -> (value, expires_at or None)
        locks = runtime.setdefault("structure_locks", {})
        guard = runtime.setdefault("structure_guard", threading.Lock())
        key = manuscript.content_sha256
        with guard:
            lock = locks.setdefault(key, threading.Lock())
        # One reading per PDF at a time: concurrent requests for the same book wait
        # for the first instead of each reading it again.
        with lock:
            with guard:
                entry = cache.get(key)
            if entry and (entry[1] is None or entry[1] > time.monotonic()):
                return entry[0]
            from src.workspace.parts import detect_parts
            try:
                value, expires = detect_parts(path), None
            except Exception as error:  # noqa: BLE001 - an unreadable PDF is reported, not a server error
                logger.info("Structure detection failed for %s: %s", manuscript.id, type(error).__name__)
                from src.evidence.extraction import PdfLimitError
                value = {"multi_part": False, "parts": [], "pages": None, "reference_lists": 0,
                         "note": str(error) if isinstance(error, PdfLimitError) else "The PDF's structure could not be read."}
                expires = time.monotonic() + STRUCTURE_FAILURE_SECONDS
            with guard:
                cache[key] = (value, expires)
                while len(cache) > 64:
                    oldest = next(iter(cache))
                    cache.pop(oldest, None)
                    if oldest != key:
                        locks.pop(oldest, None)
            return value

    @app.get("/api/v1/manuscripts/{manuscript_id}/structure", tags=["manuscript"])
    def manuscript_structure(manuscript_id: str, user: Principal):
        """Reference lists in the PDF and, for books and proceedings, the chapters they close."""
        authorize(user, "manuscript.read")
        manuscript, path = manuscript_pdf(user, manuscript_id)
        return structure_of(manuscript, path)

    @app.post("/api/v1/manuscripts/{manuscript_id}/parts", status_code=201, tags=["manuscript"])
    def split_manuscript(manuscript_id: str, body: SplitRequest, user: Principal):
        """Create one manuscript per chapter. The original PDF is kept unchanged."""
        authorize(user, "manuscript.write")
        from src.workspace.parts import part_filename, part_pdf, part_title
        manuscript, path = manuscript_pdf(user, manuscript_id)
        structure = structure_of(manuscript, path)
        if not structure["multi_part"]:
            raise HTTPException(status_code=422, detail="this PDF has a single reference list; there is nothing to split")
        wanted = set(body.parts if body.parts is not None else [part["number"] for part in structure["parts"]])
        created = []
        for part in structure["parts"]:
            if part["number"] not in wanted:
                continue
            try:
                child, _ = service.register_bytes(
                    user.tenant_id, part_pdf(path, part["page_start"], part["page_end"]),
                    original_filename=part_filename(manuscript.original_filename, part),
                    actor_id=user.actor_id, title=part_title(manuscript.title, part))
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            created.append({"part": part, "manuscript": child})
        return {"items": created, "source_manuscript_id": manuscript_id}

    # ------------------------------------------------------------------
    # plain-language assistant
    # ------------------------------------------------------------------
    def assistant_model():
        """A model for questions outside the built-in topics, or None.

        Built once per process. Any configuration problem (no key, offline
        without a local model) leaves the assistant on built-in answers.
        """
        if "assistant_model" in runtime:
            return runtime["assistant_model"]
        runtime["assistant_model"] = None
        if str(getattr(config, "ASSISTANT_MODEL", "auto")).lower() == "off":
            return None
        try:
            from pathlib import Path
            from src.evidence.providers import build_json_provider
            provider = getattr(config, "LLM_PROVIDER", "google")
            model = {"google": getattr(config, "GOOGLE_MODEL", None),
                     "openai": getattr(config, "OPENAI_MODEL", None)}.get(
                         provider, getattr(config, "OPENAI_COMPATIBLE_MODEL", None))
            client = build_json_provider(config, provider, model, 2400,
                                         Path(getattr(config, "DATA_DIR", "data")) / "debug" / "assistant_calls")
            runtime["assistant_model"] = (
                lambda prompt: client.generate(prompt, purpose="audit_assistant", prompt_version="assistant-v2"),
                f"{provider}:{model}")
        except Exception as error:  # noqa: BLE001 - built-in answers still work
            logger.info("Assistant runs without a model (%s).", type(error).__name__)
        return runtime["assistant_model"]

    def assistant_for(user, audit_id: str) -> AuditAssistant:
        authorize(user, "review.read")
        review = service.review(user.tenant_id, audit_id)
        try:
            artifact = service.load_artifact(user.tenant_id, audit_id)
        except Exception:  # noqa: BLE001 - answers still work without quoted evidence
            artifact = None
        model = assistant_model()
        return AuditAssistant(review, artifact=artifact, model=model[0] if model else None,
                              model_name=model[1] if model else None)

    @app.get("/api/v1/audits/{audit_id}/assistant", tags=["assistant"])
    def audit_assistant_summary(audit_id: str, user: Principal):
        """The opening message: a plain-language summary and suggested questions."""
        return assistant_for(user, audit_id).summary()

    @app.post("/api/v1/audits/{audit_id}/assistant", tags=["assistant"])
    def audit_assistant_answer(audit_id: str, body: AssistantQuestion, user: Principal):
        """Answer one message from this audit's results, in the context of the recent conversation."""
        history = [turn.model_dump() for turn in body.history]
        return assistant_for(user, audit_id).answer(body.question, history)

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
