"""FastAPI application factory for the Phase 3 reviewer workspace."""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from config.settings import Config
from src.webapi.auth import (
    AuthenticationError,
    AuthorizationError,
    Principal,
    authenticate,
    bearer_from_header,
)
from src.workspace.jobs import AuditJobRunner
from src.workspace.models import (
    Audit,
    AuditOptions,
    CorrectionCreate,
    CorrectionTransition,
    DecisionCreate,
    Job,
)
from src.workspace.service import ArtifactError, WorkspaceService
from src.workspace.store import (
    WorkspaceConflict,
    WorkspaceNotFound,
    WorkspaceStore,
    hydrate_audit,
    hydrate_correction,
    hydrate_job,
)


logger = logging.getLogger(__name__)


def _default_tenant() -> str:
    return os.getenv("CITEPRISM_DEFAULT_TENANT_ID", "00000000-0000-4000-8000-000000000001")


def _build_verifier():
    """Create the OIDC verifier once, and only when that mode is selected."""
    if Config.WORKSPACE_AUTH_MODE != "oidc":
        return None
    from src.webapi.oidc import build_verifier

    return build_verifier(Config)


# Phase 3 routes ask for a role; Phase 4 answers with the equivalent permission.
_PERMISSION_FOR = {
    "operator": "audit.run",
    "reviewer": "review.decide",
    "administrator": "organization.write",
}


def create_app(
    *, service: WorkspaceService | None = None, runner: AuditJobRunner | None = None
) -> FastAPI:
    runtime: dict = {}
    if service is None:
        from src.workspace.factory import (
            build_export_service,
            build_repair_service,
            build_runtime,
        )

        Config.validate_phase4()
        runtime = build_runtime(Config)
        service = runtime["service"]
        store = runtime["store"]
        if Config.RUN_EMBEDDED_WORKER:
            runner = AuditJobRunner(
                store,
                runtime["pipeline"],
                max_workers=Config.WORKSPACE_JOB_WORKERS,
                config=Config,
                pricing=runtime["pricing"],
                object_store=runtime["object_store"],
            )
        runtime["export_service"] = build_export_service(runtime)
        runtime["repair_service"] = build_repair_service(runtime, runner)
    else:
        store = service.store

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if Config.WORKSPACE_AUTH_MODE == "local":
            service.sync_legacy_documents(_default_tenant())
        if runner is not None:
            runner.start()
        yield
        if runner is not None:
            runner.shutdown(wait=False)

    app = FastAPI(
        title="CitePrism Review API",
        version="1.0.0",
        description="Human-supervised citation evidence review. Automated proposals are not misconduct findings.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.workspace_service = service
    app.state.job_runner = runner
    app.state.workspace_store = store
    app.state.runtime = runtime
    if not runtime:
        # An injected service (tests, embedding) still gets the Phase 4 surface.
        from src.workspace.exports import ExportService
        from src.workspace.objects import LocalWorkspaceObjectStore
        from src.workspace.repair import RepairService
        from src.workspace.signing import Signer

        object_root = Path(service.data_root) / "workspace_objects"
        runtime.update(
            {
                "store": store,
                "service": service,
                "object_store": LocalWorkspaceObjectStore(object_root),
                "signer": Signer.from_config(Config),
                "pricing": None,
            }
        )
        runtime["export_service"] = ExportService(
            service,
            store,
            runtime["object_store"],
            signer=runtime["signer"],
            retention_days=Config.EXPORT_RETENTION_DAYS,
        )
        runtime["repair_service"] = RepairService(service, store, runner=runner)
    verifier = _build_verifier()
    origins = [item.strip() for item in Config.WORKSPACE_CORS_ORIGINS.split(",") if item.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=[
            "Content-Type",
            "Idempotency-Key",
            "X-CitePrism-Actor",
            "X-CitePrism-Tenant",
            "X-CitePrism-Roles",
            "X-CitePrism-Proxy-Secret",
        ],
    )

    def principal(
        tenant: Annotated[str | None, Header(alias="X-CitePrism-Tenant")] = None,
        actor: Annotated[str | None, Header(alias="X-CitePrism-Actor")] = None,
        roles: Annotated[str | None, Header(alias="X-CitePrism-Roles")] = None,
        proxy_secret: Annotated[str | None, Header(alias="X-CitePrism-Proxy-Secret")] = None,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Principal:
        try:
            return authenticate(
                mode=Config.WORKSPACE_AUTH_MODE,
                tenant=tenant,
                actor=actor,
                roles=roles,
                supplied_proxy_secret=proxy_secret,
                configured_tenant=_default_tenant(),
                configured_proxy_secret=Config.WORKSPACE_PROXY_SECRET,
                bearer_token=bearer_from_header(authorization),
                verifier=verifier,
            )
        except AuthenticationError as exc:
            headers = (
                {"WWW-Authenticate": 'Bearer realm="citeprism"'}
                if Config.WORKSPACE_AUTH_MODE == "oidc"
                else None
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc), headers=headers
            ) from exc

    def require_role(value: Principal, permission: str) -> None:
        """Phase 3 call sites pass a role name; both are resolved by the matrix."""
        try:
            value.require(_PERMISSION_FOR.get(permission, permission))
        except AuthorizationError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
            ) from exc
        except ValueError as exc:  # unknown permission name
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    @app.exception_handler(WorkspaceNotFound)
    async def not_found(_request, exc):
        return JSONResponse(status_code=404, content={"code": "not_found", "detail": str(exc)})

    @app.exception_handler(WorkspaceConflict)
    async def conflict(_request, exc):
        return JSONResponse(status_code=409, content={"code": "conflict", "detail": str(exc)})

    @app.exception_handler(ArtifactError)
    async def artifact_error(_request, exc):
        return JSONResponse(
            status_code=422, content={"code": "artifact_unavailable", "detail": str(exc)}
        )

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "service": "citeprism-review-api", "api_version": "v1"}

    @app.get("/api/v1/manuscripts")
    def list_manuscripts(user: Annotated[Principal, Depends(principal)]):
        return {"items": service.list_manuscripts(user.tenant_id)}

    @app.post("/api/v1/manuscripts", status_code=201)
    async def upload_manuscript(
        user: Annotated[Principal, Depends(principal)],
        file: Annotated[UploadFile, File(description="A text-based PDF manuscript")],
        title: str | None = None,
    ):
        require_role(user, "operator")
        content = await file.read(Config.MAX_PDF_BYTES + 1)
        try:
            manuscript, created = service.register_bytes(
                user.tenant_id,
                content,
                original_filename=file.filename or "manuscript.pdf",
                actor_id=user.actor_id,
                title=title,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse(
            status_code=201 if created else 200, content=manuscript.model_dump(mode="json")
        )

    @app.get("/api/v1/manuscripts/{manuscript_id}")
    def get_manuscript(manuscript_id: str, user: Annotated[Principal, Depends(principal)]):
        return service.get_manuscript(user.tenant_id, manuscript_id)

    @app.get("/api/v1/manuscripts/{manuscript_id}/audits")
    def list_audits(manuscript_id: str, user: Annotated[Principal, Depends(principal)]):
        return {"items": service.list_audits(user.tenant_id, manuscript_id)}

    @app.post("/api/v1/manuscripts/{manuscript_id}/audits", status_code=202)
    def create_audit(
        manuscript_id: str, options: AuditOptions, user: Annotated[Principal, Depends(principal)]
    ):
        require_role(user, "operator")
        audit, job = service.create_audit(user.tenant_id, manuscript_id, options, user.actor_id)
        if runner is not None:
            runner.submit(user.tenant_id, job.id)
        return {"audit": audit, "job": job}

    @app.get("/api/v1/audits/{audit_id}")
    def get_audit(audit_id: str, user: Annotated[Principal, Depends(principal)]):
        return service.get_audit(user.tenant_id, audit_id)

    @app.get("/api/v1/audits/{audit_id}/review")
    def get_review(audit_id: str, user: Annotated[Principal, Depends(principal)]):
        return service.review(user.tenant_id, audit_id)

    @app.get("/api/v1/audits/{audit_id}/artifact")
    def get_artifact(audit_id: str, user: Annotated[Principal, Depends(principal)]):
        return service.load_artifact(user.tenant_id, audit_id)

    @app.get("/api/v1/audits/{audit_id}/references/{reference_id}")
    def get_reference_evidence(
        audit_id: str, reference_id: str, user: Annotated[Principal, Depends(principal)]
    ):
        return service.reference_evidence(user.tenant_id, audit_id, reference_id)

    @app.get("/api/v1/audits/{audit_id}/manuscript.pdf")
    def get_pdf(audit_id: str, user: Annotated[Principal, Depends(principal)]):
        audit = service.get_audit(user.tenant_id, audit_id)
        manuscript = service.get_manuscript(user.tenant_id, audit.manuscript_id)
        path = Path(manuscript.pdf_path).resolve()
        if not path.is_relative_to(service.data_root) or not path.is_file():
            raise ArtifactError("the manuscript PDF is unavailable")
        return FileResponse(
            path, media_type="application/pdf", filename=manuscript.original_filename
        )

    @app.get("/api/v1/jobs/{job_id}")
    def get_job(job_id: str, user: Annotated[Principal, Depends(principal)]):
        return service.get_job(user.tenant_id, job_id)

    @app.post("/api/v1/jobs/{job_id}/cancel", status_code=202)
    def cancel_job(job_id: str, user: Annotated[Principal, Depends(principal)]):
        require_role(user, "operator")
        row = service.store.request_cancel(user.tenant_id, job_id, user.actor_id)
        return Job.model_validate(hydrate_job(row))

    @app.post("/api/v1/jobs/{job_id}/retry", status_code=202)
    def retry_job(job_id: str, user: Annotated[Principal, Depends(principal)]):
        require_role(user, "operator")
        audit, job = service.store.retry_job(user.tenant_id, job_id, user.actor_id)
        if runner is not None:
            runner.submit(user.tenant_id, job["id"])
        return {
            "audit": Audit.model_validate(hydrate_audit(audit)),
            "job": Job.model_validate(hydrate_job(job)),
        }

    @app.get("/api/v1/review-cases")
    def review_cases(
        user: Annotated[Principal, Depends(principal)],
        priority: str | None = None,
        case_status: Annotated[str | None, Query(alias="status")] = None,
        query: str | None = None,
        limit: int = Query(default=200, ge=1, le=500),
    ):
        return {
            "items": service.review_cases(
                user.tenant_id, priority=priority, status=case_status, query=query, limit=limit
            )
        }

    @app.post("/api/v1/audits/{audit_id}/findings/{finding_id}/decisions", status_code=201)
    def record_decision(
        audit_id: str,
        finding_id: str,
        value: DecisionCreate,
        user: Annotated[Principal, Depends(principal)],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
        ],
    ):
        require_role(user, "reviewer")
        return service.record_decision(
            user.tenant_id, audit_id, finding_id, value, user.actor_id, idempotency_key
        )

    @app.post("/api/v1/audits/{audit_id}/corrections", status_code=201)
    def propose_correction(
        audit_id: str,
        value: CorrectionCreate,
        user: Annotated[Principal, Depends(principal)],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
        ],
    ):
        require_role(user, "reviewer")
        return service.propose_correction(
            user.tenant_id, audit_id, value, user.actor_id, idempotency_key
        )

    @app.post("/api/v1/corrections/{correction_id}/transition")
    def transition_correction(
        correction_id: str,
        value: CorrectionTransition,
        user: Annotated[Principal, Depends(principal)],
    ):
        require_role(user, "correction.transition")
        row = service.store.transition_correction(
            user.tenant_id, correction_id, value.state, value.rationale, user.actor_id
        )
        return hydrate_correction(row)

    @app.get("/api/v1/activity")
    def activity(
        user: Annotated[Principal, Depends(principal)],
        audit_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ):
        return {"items": service.activity(user.tenant_id, audit_id, limit)}

    @app.get("/api/v1/activity/verification")
    def activity_verification(user: Annotated[Principal, Depends(principal)]):
        return service.store.verify_activity_chain(user.tenant_id)

    from src.webapi.routes_phase4 import register_phase4_routes

    register_phase4_routes(
        app,
        principal=principal,
        service=service,
        store=store,
        config=Config,
        runtime=runtime,
    )
    return app


app = create_app()
