"""Public, versioned review contracts shared by UI, exports, and future API."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ReviewFinding(Contract):
    id: str
    reference_id: str | None = None
    category: str
    priority: Literal["high", "moderate", "low", "informational"]
    summary: str
    explanation: str
    action: str
    source_locations: list[dict] = Field(default_factory=list)
    system_issue: bool = False


class ReviewReference(Contract):
    id: str
    marker: str
    title: str
    identity_status: str
    evidence_status: str
    citation_count: int = Field(ge=0)
    claim_assessment: str
    quote_count: int = Field(ge=0)
    topic_similarity: float | None = Field(default=None, ge=0, le=100)
    finding_ids: list[str] = Field(default_factory=list)
    # V2.0 hybrid relevance (RS_final) and its explainable parts.
    relevance_score: float | None = Field(default=None, ge=0, le=100)
    relevance_label: str = "not_scored"
    relevance_basis: str = "none"
    llm_relevance: float | None = Field(default=None, ge=0, le=100)
    self_citation: str = "none"
    metadata_severity: str = "none"
    evidence_kind: str | None = None
    # As printed in the reference list: lets readers name a reference by its
    # author ("the Axelsson one") and lets reports show who wrote what.
    year: str | None = None
    authors: list[str] = Field(default_factory=list)


class ReviewProjection(Contract):
    schema_version: Literal["review-v4"] = "review-v4"
    policy_version: Literal["evidence-review-v4"] = "evidence-review-v4"
    title: str
    processing_status: Literal["completed", "completed_with_issues", "legacy_unverified"]
    review_status: Literal["not_reviewed"] = "not_reviewed"
    counts: dict[str, int]
    references: list[ReviewReference]
    findings: list[ReviewFinding]
    limitations: list[str]
    # Manuscript-level analysis summaries (self-citation pattern, literature gaps).
    analysis: dict = Field(default_factory=dict)
