"""Frozen, extra-forbidden, versioned provenance contracts."""
from datetime import datetime, timezone
from enum import Enum
import math
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SHA256_PATTERN = r"^[0-9a-f]{64}$"


class FrozenDict(dict):
    @staticmethod
    def _reject(*args, **kwargs):
        raise TypeError("trust records are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _reject
    __ior__ = _reject

    def __deepcopy__(self, memo):
        return self


def _freeze(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("trust payload numbers must be finite")
    if isinstance(value, dict):
        return FrozenDict({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


class ImmutableModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True,
                              str_strip_whitespace=True, allow_inf_nan=False)

    @field_validator("*", mode="after")
    @classmethod
    def aware_time(cls, value):
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("timestamps must be timezone-aware")
            return value.astimezone(timezone.utc)
        return value

    @model_validator(mode="after")
    def freeze_json(self):
        for field in type(self).model_fields:
            object.__setattr__(self, field, _freeze(getattr(self, field)))
        return self


class ActorType(str, Enum):
    HUMAN = "human"
    SERVICE = "service"
    MODEL = "model"


class Actor(ImmutableModel):
    actor_type: ActorType
    actor_id: str = Field(min_length=1, max_length=200)


class Record(ImmutableModel):
    record_version: Literal[1] = 1
    id: UUID
    tenant_id: UUID


class DocumentRecord(Record):
    title: str | None = Field(default=None, min_length=1, max_length=1000)
    external_key: str | None = Field(default=None, min_length=1, max_length=500)
    created_at: datetime
    created_by: str = Field(min_length=1, max_length=200)


class DocumentVersionRecord(Record):
    document_id: UUID
    version_number: int = Field(ge=1)
    parent_version_id: UUID | None = None
    content_sha256: str = Field(pattern=SHA256_PATTERN)
    object_backend: Literal["local", "s3"]
    object_key: str = Field(min_length=1, max_length=1500)
    object_version_id: str | None = Field(default=None, min_length=1, max_length=1000)
    original_filename: str = Field(min_length=1, max_length=500)
    media_type: Literal["application/pdf"] = "application/pdf"
    size_bytes: int = Field(gt=0)
    created_at: datetime
    created_by: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def parent(self):
        if (self.version_number == 1) != (self.parent_version_id is None):
            raise ValueError("version 1 must have no parent; later versions need a parent")
        return self


class SourceSpan(Record):
    document_version_id: UUID
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=1)
    text_sha256: str = Field(pattern=SHA256_PATTERN)
    locator: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    @model_validator(mode="after")
    def bounds(self):
        for start, end in ((self.page_start, self.page_end), (self.char_start, self.char_end)):
            if (start is None) != (end is None):
                raise ValueError("span bounds must be supplied in pairs")
        if self.page_start is None and self.char_start is None:
            raise ValueError("a span requires page or character bounds")
        if self.page_start is not None and self.page_end < self.page_start:
            raise ValueError("page_end precedes page_start")
        if self.char_start is not None and self.char_end <= self.char_start:
            raise ValueError("char_end must exceed char_start")
        return self


class ReferenceRecord(Record):
    document_version_id: UUID
    reference_key: str = Field(min_length=1, max_length=300)
    ordinal: int = Field(ge=1)
    source_span_id: UUID | None = None
    raw_text_sha256: str = Field(pattern=SHA256_PATTERN)
    bibliographic_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ModelRunStatus(str, Enum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ModelRun(Record):
    document_version_id: UUID
    purpose: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=300)
    prompt_version: str = Field(min_length=1, max_length=200)
    method_version: str = Field(min_length=1, max_length=200)
    input_sha256: str = Field(pattern=SHA256_PATTERN)
    output_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    configuration: dict[str, Any] = Field(default_factory=dict)
    status: ModelRunStatus
    started_at: datetime
    completed_at: datetime | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def completion(self):
        if self.status == ModelRunStatus.STARTED:
            if any(x is not None for x in (self.completed_at, self.output_sha256, self.error_code)):
                raise ValueError("started runs cannot contain completion evidence")
        elif self.status == ModelRunStatus.SUCCEEDED:
            if self.completed_at is None or self.output_sha256 is None or self.error_code is not None:
                raise ValueError("succeeded runs need time/output and no error")
        elif self.completed_at is None or self.error_code is None:
            raise ValueError("failed runs need completion time and error code")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completion precedes start")
        return self


class CandidateMatch(Record):
    document_version_id: UUID
    reference_id: UUID
    model_run_id: UUID | None = None
    provider: str = Field(min_length=1, max_length=100)
    external_id: str = Field(min_length=1, max_length=1000)
    match_method: str = Field(min_length=1, max_length=200)
    confidence: float | None = Field(default=None, ge=0, le=1)
    selected: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class EvidenceKind(str, Enum):
    SOURCE_QUOTE = "source_quote"
    METADATA_MATCH = "metadata_match"
    SCORE_COMPONENT = "score_component"
    MODEL_RATIONALE = "model_rationale"
    AUTHOR_OVERLAP = "author_overlap"
    POLICY_CHECK = "policy_check"


class Evidence(Record):
    document_version_id: UUID
    subject_type: Literal["document_version", "source_span", "reference", "candidate_match", "model_run", "finding"]
    subject_id: UUID
    kind: EvidenceKind
    source_span_id: UUID | None = None
    candidate_match_id: UUID | None = None
    model_run_id: UUID | None = None
    statement: str = Field(min_length=1, max_length=4000)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ReviewPriority(str, Enum):
    INFORMATIONAL = "informational"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class FindingStatus(str, Enum):
    OPEN = "open"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"
    SUPERSEDED = "superseded"


class Finding(Record):
    document_version_id: UUID
    reference_id: UUID | None = None
    model_run_id: UUID | None = None
    category: str = Field(min_length=1, max_length=200)
    priority: ReviewPriority
    status: FindingStatus = FindingStatus.OPEN
    summary: str = Field(min_length=1, max_length=2000)
    details: dict[str, Any] = Field(default_factory=dict)
    policy_version: str = Field(min_length=1, max_length=200)
    created_at: datetime


class ReviewDisposition(str, Enum):
    CONFIRM = "confirm"
    DISMISS = "dismiss"
    ESCALATE = "escalate"
    NEEDS_MORE_EVIDENCE = "needs_more_evidence"


class ReviewerDecision(Record):
    finding_id: UUID
    reviewer_id: str = Field(min_length=1, max_length=200)
    disposition: ReviewDisposition
    rationale: str = Field(min_length=1, max_length=4000)
    supersedes_decision_id: UUID | None = None
    created_at: datetime


class AuditEvent(Record):
    sequence_number: int = Field(ge=1)
    occurred_at: datetime
    actor: Actor
    action: str = Field(min_length=1, max_length=200)
    aggregate_type: str = Field(min_length=1, max_length=100)
    aggregate_id: UUID
    payload: dict[str, Any]
    previous_hash: str = Field(pattern=SHA256_PATTERN)
    event_hash: str = Field(pattern=SHA256_PATTERN)


class StoredObject(ImmutableModel):
    backend: Literal["local", "s3"]
    key: str
    content_sha256: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(gt=0)
    version_id: str | None = None


class IngestReceipt(ImmutableModel):
    tenant_id: UUID
    document_id: UUID
    document_version_id: UUID
    content_sha256: str
    object_backend: str
    object_key: str
    object_version_id: str | None = None
    reused: bool


class LegacyCaptureReceipt(ImmutableModel):
    tenant_id: UUID
    document_version_id: UUID
    artifact_sha256: str
    model_run_id: UUID
    references_created: int
    candidate_matches_created: int
    evidence_created: int
    findings_created: int


class AuditVerification(ImmutableModel):
    valid: bool
    event_count: int
    head_hash: str | None = None
    error: str | None = None
