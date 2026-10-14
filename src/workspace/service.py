"""Application service joining Phase 2 artifacts to Phase 3 workflow state."""

from __future__ import annotations

import copy

import json
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

from src.evidence.review import build_review
from src.utils.safe_io import (
    atomic_copy_file,
    content_addressed_pdf_name,
    sha256_file,
    validate_pdf,
)
from src.workspace.models import (
    Activity,
    Audit,
    AuditOptions,
    Correction,
    CorrectionCreate,
    Decision,
    DecisionCreate,
    Job,
    Manuscript,
)
from src.workspace.store import (
    WorkspaceConflict,
    WorkspaceStore,
    decode,
    hydrate_activity,
    hydrate_audit,
    hydrate_correction,
    hydrate_decision,
    hydrate_job,
    hydrate_manuscript,
)


class ArtifactError(RuntimeError):
    pass


def _safe_error(error: BaseException) -> tuple[str, str]:
    """Map internal/provider failures to stable client-safe diagnostic text."""
    code = getattr(error, "code", None) or type(error).__name__.lower()
    text = str(error).strip() or "The operation could not be completed."
    lowered = text.lower()
    if any(secret in lowered for secret in ("api key", "token", "password", "authorization")):
        text = "Provider authentication failed. Check the server-side credential configuration."
        code = "authentication"
    text = re.sub(r"[A-Za-z]:\\[^\n\r]+", "[server path]", text)
    text = re.sub(r"(?<!\w)/(?:[^\s/]+/){2,}[^\s]+", "[server path]", text)
    return str(code)[:100], text[:1000]


class WorkspaceService:
    def __init__(
        self,
        store: WorkspaceStore,
        *,
        data_root: str | Path,
        max_pdf_bytes: int = 50 * 1024 * 1024,
        legacy_db=None,
    ):
        self.store = store
        self.data_root = Path(data_root).resolve()
        self.upload_root = self.data_root / "workspace_uploads"
        self.max_pdf_bytes = max_pdf_bytes
        self.legacy_db = legacy_db

    def sync_legacy_documents(self, tenant_id: str, actor_id: str = "phase3-migration") -> int:
        """Expose existing local Phase 2 manuscripts without requiring another upload."""
        if self.legacy_db is None:
            return 0
        imported = 0
        for value in self.legacy_db.list_all_documents():
            status = self.legacy_db.get_document_status(value["id"])
            if not status or not status.get("pdf_path"):
                continue
            path = Path(str(status["pdf_path"]).replace("\\", "/"))
            if not path.is_absolute():
                path = self.data_root.parent / path
            if not path.is_file():
                continue
            _record, created = self.register_pdf(
                tenant_id,
                path,
                original_filename=value.get("display_filename") or path.name,
                title=value.get("title"),
                actor_id=actor_id,
            )
            imported += int(created)
        return imported

    def register_pdf(
        self,
        tenant_id: str,
        source: str | Path,
        *,
        original_filename: str,
        actor_id: str,
        title: str | None = None,
    ) -> tuple[Manuscript, bool]:
        source = Path(source)
        validate_pdf(source, max_bytes=self.max_pdf_bytes)
        digest = sha256_file(source)
        destination = self.upload_root / content_addressed_pdf_name(original_filename, digest)
        if not destination.exists():
            atomic_copy_file(source, destination)
        elif sha256_file(destination) != digest:
            raise ArtifactError("stored manuscript failed its integrity check")
        legacy_id = None
        if self.legacy_db is not None:
            legacy_id, _ = self.legacy_db.register_pdf(
                destination, original_filename=original_filename
            )
        # Shown in the library and in reports: no control characters, invisible
        # characters or bidirectional overrides ("invoice\u202efdp.exe").
        cleaned_name = re.sub(r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "",
                              str(original_filename))
        display_name = Path(cleaned_name.replace("\\", "/")).name[:500]
        if not display_name or display_name in {".", ".."}:
            display_name = "manuscript.pdf"
        display_title = (
            str(title or Path(display_name).stem).strip()[:1000] or "Untitled manuscript"
        )
        row, created = self.store.upsert_manuscript(
            tenant_id,
            title=display_title,
            original_filename=display_name,
            content_sha256=digest,
            pdf_path=str(destination),
            legacy_document_id=legacy_id,
            actor_id=actor_id,
        )
        return Manuscript.model_validate(hydrate_manuscript(row)), created

    def register_bytes(
        self,
        tenant_id: str,
        content: bytes,
        *,
        original_filename: str,
        actor_id: str,
        title: str | None = None,
    ) -> tuple[Manuscript, bool]:
        if len(content) > self.max_pdf_bytes:
            raise ValueError("The uploaded PDF exceeds the configured size limit.")
        with tempfile.TemporaryDirectory(prefix="citeprism-upload-") as directory:
            source = Path(directory) / "upload.pdf"
            source.write_bytes(content)
            return self.register_pdf(
                tenant_id,
                source,
                original_filename=original_filename,
                actor_id=actor_id,
                title=title,
            )

    def list_manuscripts(self, tenant_id: str) -> list[Manuscript]:
        result = []
        for raw in self.store.list_manuscripts(tenant_id):
            row = hydrate_manuscript(raw)
            if raw.get("latest_audit_id"):
                summary = self.store.get_audit(tenant_id, raw["latest_audit_id"])
                counts = decode(summary.get("summary_json"), {})
                row["open_findings"] = self._open_references(tenant_id, raw["latest_audit_id"], counts, raw)
            result.append(Manuscript.model_validate(row))
        return result

    def _open_references(self, tenant_id: str, audit_id: str, counts: dict, raw: dict) -> int:
        """References that still have an undecided item a person should look at.

        Superseded decisions, decisions on notes for information and repeated
        decisions on one finding never reduce the count.
        """
        mapping = counts.get("attention_findings")
        if isinstance(mapping, dict):
            decided = set(self.store.latest_decisions(tenant_id, audit_id))
            return len({reference for finding, reference in mapping.items() if finding not in decided})
        ids = counts.get("attention_finding_ids")
        if isinstance(ids, list):
            decided = set(self.store.latest_decisions(tenant_id, audit_id))
            open_ids = set(ids) - decided
            if not open_ids:
                return 0
            try:
                review = self.review(tenant_id, audit_id)
            except Exception:  # noqa: BLE001 - an unreadable artifact still gets a count
                return len(open_ids)
            return len({finding["reference_id"] for finding in review["findings"] if finding["id"] in open_ids})
        # Audits from before 5.4 lack the finding ids: an approximation, never negative.
        attention = counts.get("attention_references", counts.get("references_to_review", 0))
        return max(0, int(attention) - int(raw.get("decision_count", 0)))

    def get_manuscript(self, tenant_id: str, manuscript_id: str) -> Manuscript:
        row = hydrate_manuscript(self.store.get_manuscript(tenant_id, manuscript_id))
        audits = self.store.list_audits(tenant_id, manuscript_id)
        if audits:
            row["latest_audit_id"] = audits[0]["id"]
            row["latest_audit_state"] = audits[0]["state"]
        return Manuscript.model_validate(row)

    def create_audit(
        self, tenant_id: str, manuscript_id: str, options: AuditOptions, actor_id: str
    ) -> tuple[Audit, Job]:
        manuscript = self.store.get_manuscript(tenant_id, manuscript_id)
        request = {
            "pdf_path": manuscript["pdf_path"],
            "original_filename": manuscript["original_filename"],
            "options": options.model_dump(mode="json"),
        }
        audit, job = self.store.create_audit_job(tenant_id, manuscript_id, request, actor_id)
        return (Audit.model_validate(hydrate_audit(audit)), Job.model_validate(hydrate_job(job)))

    def get_audit(self, tenant_id: str, audit_id: str) -> Audit:
        return Audit.model_validate(hydrate_audit(self.store.get_audit(tenant_id, audit_id)))

    def list_audits(self, tenant_id: str, manuscript_id: str) -> list[Audit]:
        return [
            Audit.model_validate(hydrate_audit(row))
            for row in self.store.list_audits(tenant_id, manuscript_id)
        ]

    def get_job(self, tenant_id: str, job_id: str) -> Job:
        return Job.model_validate(hydrate_job(self.store.get_job(tenant_id, job_id)))

    def load_artifact(self, tenant_id: str, audit_id: str) -> dict[str, Any]:
        audit = self.store.get_audit(tenant_id, audit_id)
        path_value = audit.get("artifact_path")
        if not path_value:
            raise ArtifactError("the audit does not have a completed evidence artifact")
        path = Path(path_value).resolve()
        allowed = self.data_root
        if not path.is_relative_to(allowed) or not path.is_file():
            raise ArtifactError("the evidence artifact is unavailable")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactError("the evidence artifact could not be read") from exc
        if not isinstance(payload, dict):
            raise ArtifactError("the evidence artifact has an invalid root")
        return payload

    def _projection(self, tenant_id: str, audit_id: str) -> dict[str, Any]:
        """The review built from an audit's artifact, cached while the artifact file is unchanged;
        decisions and corrections are merged on every call."""
        cache = self.__dict__.setdefault("_projections", {})
        lock = self.__dict__.setdefault("_projections_lock", threading.Lock())
        # Keyed on the file's state too: audits from before 5.4 share one file per document,
        # which a later audit of the same paper rewrites.
        try:
            stat = Path(self.store.get_audit(tenant_id, audit_id).get("artifact_path") or "").stat()
            version = (stat.st_size, stat.st_mtime_ns)
        except (OSError, TypeError, ValueError):
            version = None
        key = (tenant_id, audit_id, version)
        with lock:
            cached = cache.get(key)
        if cached is None:
            cached = build_review(self.load_artifact(tenant_id, audit_id))
            with lock:
                while len(cache) >= 32:
                    cache.pop(next(iter(cache)), None)
                cache[key] = cached
        return copy.deepcopy(cached)

    def review(self, tenant_id: str, audit_id: str) -> dict[str, Any]:
        review = self._projection(tenant_id, audit_id)
        decisions = self.store.latest_decisions(tenant_id, audit_id)
        corrections = [
            hydrate_correction(row) for row in self.store.list_corrections(tenant_id, audit_id)
        ]
        for finding in review["findings"]:
            finding["decision"] = decisions.get(finding["id"])
        review["counts"]["reviewer_decisions"] = len(decisions)
        review["counts"]["open_findings"] = sum(
            finding["id"] not in decisions for finding in review["findings"]
        )
        review["review_status"] = (
            "not_reviewed"
            if not decisions
            else "reviewed"
            if not review["counts"]["open_findings"]
            else "in_review"
        )
        review["corrections"] = corrections
        return review

    def reference_evidence(
        self, tenant_id: str, audit_id: str, reference_id: str
    ) -> dict[str, Any]:
        artifact = self.load_artifact(tenant_id, audit_id)
        review = self.review(tenant_id, audit_id)
        reference = next(
            (item for item in review["references"] if item["id"] == reference_id), None
        )
        source = next(
            (
                item
                for item in artifact.get("scored_references", [])
                if (item.get("reference_uid") == reference_id)
            ),
            None,
        )
        if reference is None or source is None:
            raise WorkspaceConflict("reference does not belong to this audit")
        findings = [item for item in review["findings"] if item["reference_id"] == reference_id]
        return {
            "reference": reference,
            "as_cited": source.get("original_data", {}),
            "resolution": source.get("resolution", {}),
            "source_snapshot": source.get("source_snapshot"),
            "claim_assessments": source.get("claim_assessments", []),
            "external_metadata": source.get("external_metadata", {}),
            "findings": findings,
        }

    def record_decision(
        self,
        tenant_id: str,
        audit_id: str,
        finding_id: str,
        value: DecisionCreate,
        reviewer_id: str,
        idempotency_key: str,
    ) -> Decision:
        valid_findings = {
            finding["id"]
            for finding in build_review(self.load_artifact(tenant_id, audit_id))["findings"]
        }
        if finding_id not in valid_findings:
            raise WorkspaceConflict("finding does not belong to this audit projection")
        row = self.store.record_decision(
            tenant_id,
            audit_id,
            finding_id,
            value.disposition.value,
            value.rationale,
            reviewer_id,
            idempotency_key,
            value.expected_current_decision_id,
        )
        return Decision.model_validate(hydrate_decision(row))

    def propose_correction(
        self,
        tenant_id: str,
        audit_id: str,
        value: CorrectionCreate,
        reviewer_id: str,
        idempotency_key: str,
    ) -> Correction:
        reference_ids = {
            ref["id"] for ref in build_review(self.load_artifact(tenant_id, audit_id))["references"]
        }
        if not set(value.reference_ids).issubset(reference_ids):
            raise WorkspaceConflict("one or more references do not belong to this audit")
        if value.kind.value == "merge_references" and len(value.reference_ids) < 2:
            raise ValueError("a merge proposal requires at least two references")
        row = self.store.record_correction(
            tenant_id,
            audit_id,
            kind=value.kind.value,
            reference_ids=value.reference_ids,
            proposed_value=value.proposed_value,
            rationale=value.rationale,
            reviewer_id=reviewer_id,
            idempotency_key=idempotency_key,
            supersedes_id=value.supersedes_correction_id,
        )
        return Correction.model_validate(hydrate_correction(row))

    def activity(
        self, tenant_id: str, audit_id: str | None = None, limit: int = 100
    ) -> list[Activity]:
        return [
            Activity.model_validate(row)
            for row in hydrate_activity(self.store.list_activity(tenant_id, audit_id, limit))
        ]

    def review_cases(
        self,
        tenant_id: str,
        *,
        priority: str | None = None,
        status: str | None = None,
        query: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        cases = []
        normalized_query = (query or "").casefold()
        for audit in self.store.list_review_cases(tenant_id, limit=limit):
            review = self.review(tenant_id, audit["id"])
            for finding in review["findings"]:
                decision = finding.get("decision")
                case_status = "decided" if decision else "open"
                if priority and finding["priority"] != priority:
                    continue
                if status and case_status != status:
                    continue
                haystack = " ".join(
                    (
                        audit["manuscript_title"],
                        finding["summary"],
                        finding["explanation"],
                        finding["category"],
                    )
                ).casefold()
                if normalized_query and normalized_query not in haystack:
                    continue
                cases.append(
                    {
                        **finding,
                        "audit_id": audit["id"],
                        "manuscript_id": audit["manuscript_id"],
                        "manuscript_title": audit["manuscript_title"],
                        "case_status": case_status,
                    }
                )
        ordering = {"high": 0, "moderate": 1, "low": 2, "informational": 3}
        return sorted(
            cases,
            key=lambda item: (
                item["case_status"] != "open",
                ordering[item["priority"]],
                item["manuscript_title"],
            ),
        )[:limit]


__all__ = ["ArtifactError", "WorkspaceService", "_safe_error"]
