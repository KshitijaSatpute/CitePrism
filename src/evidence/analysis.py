"""Deterministic post-scoring analysis: relevance, consistency, self-citation, gaps.

Everything here is computed from what the pipeline already recorded — grounded
assessments, topic similarity, resolved metadata — so it is cheap and is re-run
on every audit, including audits that reuse a cached scoring artifact. That is
what lets an operator change a threshold or supply author ORCID iDs and see the
effect without paying for another round of model calls.

The only network access is the optional literature-gap analyzer, which uses the
same rate-limited, recorded transport as enrichment and caches its results.
"""

from __future__ import annotations

import copy
import logging

from src.evidence.consistency import CONSISTENCY_VERSION, assess_consistency
from src.evidence.relevance import RELEVANCE_VERSION, assess_relevance
from src.evidence.self_citation import (
    SELF_CITATION_VERSION,
    analyze_self_citations,
    front_matter_orcids,
)

logger = logging.getLogger(__name__)

ANALYSIS_VERSION = "evidence-analysis-v1"


def _float(config, name, default):
    try:
        return float(getattr(config, name, default))
    except (TypeError, ValueError):
        return default


def analyze_artifact(data: dict, config, *, author_orcids=None, gap_analyzer=None) -> dict:
    """Return a copy of a scored artifact with every analysis layer recomputed."""
    from src.evidence.review import build_review
    from src.evidence.providers import utc_now

    result = copy.deepcopy(data)
    references = result.get("scored_references", [])

    for reference in references:
        relevance = assess_relevance(
            reference.get("claim_assessments") or [], reference.get("topic_similarity"), config
        )
        reference["relevance"] = relevance
        # The V2.0 field names, restored as real values rather than nulls.
        reference["RS_llm"] = relevance["RS_llm"]
        reference["RS_embed"] = relevance["RS_embed"]
        reference["RS_final"] = relevance["RS_final"]
        reference["relevance_label"] = relevance["label"]
        reference["metadata_consistency"] = assess_consistency(reference)

    manuscript = result.get("manuscript_metadata") or result.get("metadata") or {}
    supplied = list(author_orcids or manuscript.get("supplied_orcids") or [])
    analysis = analyze_self_citations(
        manuscript.get("authors") or [],
        references,
        supplied_orcids=supplied,
        front_matter=front_matter_orcids(result.get("source_document")),
        rate_threshold=_float(config, "SELF_CITATION_RATE_THRESHOLD", 0.25),
        minimum_count=int(_float(config, "SELF_CITATION_MIN_COUNT", 3)),
        concentration_threshold=_float(config, "SELF_CITATION_CONCENTRATION", 0.15),
    )
    per_reference = analysis.pop("per_reference")
    for reference in references:
        uid = reference.get("reference_uid") or str(reference.get("ref_id"))
        record = per_reference.get(uid) or {"is_self_citation": False, "tier": "none", "matches": []}
        # `is_self_cite` keeps the Streamlit diagnostics working unchanged.
        reference["self_citation"] = {**record, "is_self_cite": record["is_self_citation"],
                                      "method": SELF_CITATION_VERSION}
    if supplied:
        result.setdefault("manuscript_metadata", {})["supplied_orcids"] = supplied
    result["self_citation_analysis"] = analysis

    if gap_analyzer is not None:
        try:
            result["literature_gap_analysis"] = gap_analyzer.analyze(result)
        except Exception:  # noqa: BLE001 - optional layer
            logger.exception("Literature gap analysis failed; the audit continues without it.")

    result["analysis_metadata"] = {
        "method_version": ANALYSIS_VERSION,
        "relevance_version": RELEVANCE_VERSION,
        "consistency_version": CONSISTENCY_VERSION,
        "self_citation_version": SELF_CITATION_VERSION,
        "generated_at": utc_now(),
        "thresholds": {
            "relevant": _float(config, "RELEVANT_THRESHOLD", 70),
            "borderline": _float(config, "BORDERLINE_THRESHOLD", 40),
            "self_citation_rate": _float(config, "SELF_CITATION_RATE_THRESHOLD", 0.25),
        },
    }
    result["review"] = build_review(result)
    if "scoring_summary" in result:
        result["scoring_summary"] = result["review"]["counts"]
    return result


__all__ = ["ANALYSIS_VERSION", "analyze_artifact"]
