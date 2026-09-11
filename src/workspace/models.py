"""Strict public contracts for the Phase 3 operational workspace."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


class AuditState(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    COMPLETED_WITH_ISSUES = "completed_with_issues"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DecisionDisposition(str, Enum):
    CONFIRM = "confirm"
    DISMISS = "dismiss"
    ESCALATE = "escalate"
    NEEDS_MORE_EVIDENCE = "needs_more_evidence"


class CorrectionKind(str, Enum):
    METADATA = "metadata"
    MERGE_REFERENCES = "merge_references"
    SPLIT_REFERENCE = "split_reference"
    REASSIGN_CITATION = "reassign_citation"


class CorrectionState(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class Manuscript(Contract):
    id: str
    title: str
    original_filename: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pdf_path: str
    # Phase 4: where the manuscript bytes live, and whether the PDF carried a
    # usable text layer. Defaults keep every Phase 3 payload valid.
    object_backend: str = "local"
    object_key: str | None = None
    text_layer_status: Literal[
        "unknown", "text", "needs_ocr", "ocr_applied", "ocr_failed"
    ] = "unknown"
    legacy_document_id: int | None = None
    latest_audit_id: str | None = None
    latest_audit_state: AuditState | None = None
    open_findings: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime


class Audit(Contract):
    id: str
    manuscript_id: str
    job_id: str
    state: AuditState
    artifact_path: str | None = None
    # Phase 4: the same evidence artifact published to object storage.
    artifact_key: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class Job(Contract):
    id: str
    audit_id: str
    state: JobState
    progress: int = Field(ge=0, le=100)
    stage: str
    attempts: int = Field(ge=0)
    cancel_requested: bool = False
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class DecisionCreate(Contract):
    disposition: DecisionDisposition
    rationale: str = Field(min_length=3, max_length=4000)
    expected_current_decision_id: str | None = None


class Decision(Contract):
    id: str
    audit_id: str
    finding_id: str
    disposition: DecisionDisposition
    rationale: str
    reviewer_id: str
    supersedes_decision_id: str | None = None
    created_at: datetime


class CorrectionCreate(Contract):
    kind: CorrectionKind
    reference_ids: list[str] = Field(min_length=1, max_length=20)
    proposed_value: dict[str, Any]
    rationale: str = Field(min_length=3, max_length=4000)
    supersedes_correction_id: str | None = None

    @field_validator("reference_ids")
    @classmethod
    def unique_references(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("reference_ids must be unique")
        return value


class Correction(Contract):
    id: str
    audit_id: str
    kind: CorrectionKind
    reference_ids: list[str]
    proposed_value: dict[str, Any]
    rationale: str
    reviewer_id: str
    state: CorrectionState
    supersedes_correction_id: str | None = None
    created_at: datetime


class CorrectionTransition(Contract):
    state: Literal["accepted", "rejected"]
    rationale: str = Field(min_length=3, max_length=4000)


class AuditOptions(Contract):
    force_parse: bool = False
    force_enrich: bool = False
    force_score: bool = False
    run_missing_citations: bool = False


class Activity(Contract):
    id: int
    audit_id: str | None = None
    actor_id: str
    action: str
    entity_type: str
    entity_id: str
    payload: dict[str, Any]
    previous_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
