"""Trusted model/table allowlist; identifiers never come from a user."""
from src.trust.models import (
    CandidateMatch, DocumentRecord, DocumentVersionRecord, Evidence, Finding,
    ModelRun, ReferenceRecord, ReviewerDecision, SourceSpan,
)

MODELS = {
    "document": ("documents", DocumentRecord),
    "document_version": ("document_versions", DocumentVersionRecord),
    "source_span": ("source_spans", SourceSpan),
    "reference": ("citation_references", ReferenceRecord),
    "model_run": ("model_runs", ModelRun),
    "candidate_match": ("candidate_matches", CandidateMatch),
    "finding": ("findings", Finding),
    "evidence": ("evidence", Evidence),
    "reviewer_decision": ("reviewer_decisions", ReviewerDecision),
}
RUNTIME_TABLES = ("tenants", *(v[0] for v in MODELS.values()), "audit_events")


def kind_of(record):
    for kind, (_, model) in MODELS.items():
        if type(record) is model:
            return kind
    raise TypeError(f"unsupported trust record: {type(record).__name__}")
