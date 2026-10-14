"""Join a CitePrism evidence artifact to a labeled dataset and score it.

The runner never re-runs the pipeline. It reads artifacts the pipeline already
produced, projects them through the same `build_review` used by the UI, and
compares that projection with human labels — so an evaluation measures what
reviewers actually see, not an internal value that never reaches the screen.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from src.evaluation.dataset import EvaluationDataset, LabeledItem, load_dataset
from src.evaluation.metrics import (
    agreement,
    binary_report,
    brier_score,
    calibration_bins,
    classification_report,
    expected_calibration_error,
    maximum_calibration_error,
)
from src.evidence.text import canonical_id

logger = logging.getLogger(__name__)

# Projected identity status -> the label vocabulary an annotator uses.
IDENTITY_PROJECTION = {
    "resolved": "resolved_correct",
    "not_found": "unresolvable",
    "unresolved": "unresolvable",
    "ambiguous": "unresolvable",
    "insufficient_metadata": "unresolvable",
    "provider_unavailable": "not_applicable",
    "extraction_needs_review": "not_applicable",
}

SUPPORT_PROJECTION = {
    "supports": "supports",
    "contradicts": "contradicts",
    "context_only": "context_only",
    "unrelated": "unrelated",
    "insufficient_evidence": "insufficient_evidence",
    "not_assessed": "unassessable",
}

EXTRACTION_PROJECTION = {"located": "clean", "needs_review": "needs_review"}


def _reference_index(artifact: dict) -> dict[str, dict]:
    """Index scored references by their printed marker, e.g. '12'."""
    index: dict[str, dict] = {}
    for reference in artifact.get("scored_references", []):
        key = canonical_id(reference.get("ref_id"))
        if key:
            index[key] = reference
    return index


def _predicted_identity(reference: dict) -> str:
    status = (reference.get("resolution") or {}).get("status", "unresolved")
    return IDENTITY_PROJECTION.get(status, "unresolvable")


def _predicted_support(reference: dict) -> str:
    decisions = [
        item.get("decision")
        for item in reference.get("claim_assessments", [])
        if item.get("status") == "grounded_proposal" and item.get("decision")
    ]
    if not decisions:
        return "unassessable"
    for priority in ("contradicts", "supports", "unrelated", "context_only", "insufficient_evidence"):
        if priority in decisions:
            return SUPPORT_PROJECTION[priority]
    return "unassessable"


def _predicted_extraction(reference: dict) -> str:
    status = (reference.get("original_data") or {}).get("extraction_status", "needs_review")
    return EXTRACTION_PROJECTION.get(status, "unlocatable")


def _identity_confidence(reference: dict) -> float | None:
    """The strongest candidate's rank strength, treated as a stated confidence.

    The resolver documents that rank strength is not a calibrated probability.
    Measuring it here is exactly how that claim gets tested rather than assumed.
    """
    candidates = (reference.get("resolution") or {}).get("candidates") or []
    if not candidates:
        return None
    selected = next((item for item in candidates if item.get("selected")), candidates[0])
    strength = selected.get("rank_strength")
    try:
        return min(1.0, max(0.0, float(strength)))
    except (TypeError, ValueError):
        return None


RELEVANCE_PROJECTION = {
    "relevant": "relevant",
    "borderline": "borderline",
    "potentially_irrelevant": "irrelevant",
    "low_topical_similarity": "borderline",
}


def _relevance(reference: dict) -> dict:
    return reference.get("relevance") or {}


def evaluate_audit(artifact: dict, items: list[LabeledItem], *, bins: int = 10) -> dict:
    """Score one artifact against the labels for that manuscript."""
    from src.evidence.review import build_review

    if artifact.get("schema_version") and "relevance" not in (
        (artifact.get("scored_references") or [{}])[0] if artifact.get("scored_references") else {}
    ):
        # Artifacts scored before the analysis layer existed get it computed
        # here, deterministically, so they can be evaluated on V2.0 criteria.
        from types import SimpleNamespace

        from src.evidence.analysis import analyze_artifact
        artifact = analyze_artifact(artifact, SimpleNamespace())
    review = build_review(artifact)
    references = _reference_index(artifact)
    surfaced = {
        canonical_id(reference["marker"])
        for reference in review["references"]
        if reference["finding_ids"]
    }

    identity_pairs: list[tuple[str, str]] = []
    support_pairs: list[tuple[str, str]] = []
    extraction_pairs: list[tuple[str, str]] = []
    triage_pairs: list[tuple[bool, bool]] = []
    confidence_pairs: list[tuple[float, bool]] = []
    relevance_pairs: list[tuple[str, str]] = []
    relevance_scores: list[tuple[float, bool]] = []
    self_citation_pairs: list[tuple[bool, bool]] = []
    metadata_pairs: list[tuple[bool, bool]] = []
    unmatched: list[str] = []

    for item in items:
        key = canonical_id(item.reference_marker)
        reference = references.get(key)
        if reference is None:
            unmatched.append(item.item_id)
            continue
        if item.identity_label:
            predicted = _predicted_identity(reference)
            identity_pairs.append((item.identity_label, predicted))
            confidence = _identity_confidence(reference)
            if confidence is not None:
                confidence_pairs.append(
                    (confidence, item.identity_label == "resolved_correct")
                )
        if item.support_label:
            support_pairs.append((item.support_label, _predicted_support(reference)))
        if item.extraction_label:
            extraction_pairs.append((item.extraction_label, _predicted_extraction(reference)))
        if item.should_be_reviewed is not None:
            triage_pairs.append((bool(item.should_be_reviewed), key in surfaced))
        relevance = _relevance(reference)
        if item.relevance_label:
            predicted = RELEVANCE_PROJECTION.get(relevance.get("label"))
            if predicted is not None:
                relevance_pairs.append((item.relevance_label, predicted))
            if relevance.get("RS_final") is not None:
                relevance_scores.append(
                    (float(relevance["RS_final"]), item.relevance_label == "irrelevant")
                )
        if item.is_self_citation is not None:
            predicted = bool((reference.get("self_citation") or {}).get("is_self_citation"))
            self_citation_pairs.append((bool(item.is_self_citation), predicted))
        if item.metadata_label:
            severity = (reference.get("metadata_consistency") or {}).get("severity", "none")
            metadata_pairs.append((item.metadata_label == "inconsistent", severity in {"major", "minor"}))

    return {
        "manuscript_title": review["title"],
        "labeled_items": len(items),
        "matched_items": len(items) - len(unmatched),
        "unmatched_item_ids": unmatched,
        "identity": classification_report(identity_pairs) if identity_pairs else None,
        "support": classification_report(support_pairs) if support_pairs else None,
        "extraction": classification_report(extraction_pairs) if extraction_pairs else None,
        "triage": binary_report(triage_pairs) if triage_pairs else None,
        "relevance": classification_report(relevance_pairs) if relevance_pairs else None,
        "self_citation": binary_report(self_citation_pairs) if self_citation_pairs else None,
        "metadata": binary_report(metadata_pairs) if metadata_pairs else None,
        "calibration": (
            {
                "samples": len(confidence_pairs),
                "signal": "identity rank_strength",
                "brier_score": brier_score(confidence_pairs),
                "expected_calibration_error": expected_calibration_error(
                    confidence_pairs, bins=bins
                ),
                "maximum_calibration_error": maximum_calibration_error(
                    confidence_pairs, bins=bins
                ),
                "reliability": calibration_bins(confidence_pairs, bins=bins),
            }
            if confidence_pairs
            else None
        ),
        "_pairs": {
            "identity": identity_pairs,
            "support": support_pairs,
            "extraction": extraction_pairs,
            "triage": triage_pairs,
            "confidence": confidence_pairs,
            "relevance": relevance_pairs,
            "relevance_scores": relevance_scores,
            "self_citation": self_citation_pairs,
            "metadata": metadata_pairs,
        },
    }


def evaluate_dataset(
    dataset: EvaluationDataset | str | Path,
    artifacts: dict[str, dict] | list[str | Path],
    *,
    bins: int = 10,
) -> dict:
    """Score every manuscript in a dataset and aggregate the results.

    `artifacts` is either a mapping of manuscript SHA-256 to loaded artifact, or
    a list of artifact paths whose `source_document.pdf_sha256` supplies the key.
    """
    if not isinstance(dataset, EvaluationDataset):
        dataset = load_dataset(dataset)
    problems = dataset.validate()
    if problems:
        raise ValueError("dataset validation failed:\n" + "\n".join(f"  - {p}" for p in problems))

    resolved: dict[str, dict] = {}
    if isinstance(artifacts, dict):
        resolved = dict(artifacts)
    else:
        for path in artifacts:
            try:
                document = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logger.warning("Skipping unreadable artifact: %s", path)
                continue
            digest = (document.get("source_document") or {}).get("pdf_sha256")
            if digest:
                resolved[digest] = document

    per_manuscript = []
    keys = ("identity", "support", "extraction", "triage", "confidence", "relevance",
            "relevance_scores", "self_citation", "metadata")
    combined: dict[str, list] = {key: [] for key in keys}
    groups: list[dict] = []  # per-manuscript pairs, for the cluster bootstrap
    by_domain: dict[str, dict[str, list]] = {}
    missing: list[str] = []

    for digest in sorted({item.manuscript_sha256 for item in dataset.items}):
        artifact = resolved.get(digest)
        if artifact is None:
            missing.append(digest)
            continue
        items = dataset.by_manuscript(digest)
        result = evaluate_audit(artifact, items, bins=bins)
        pairs = result.pop("_pairs")
        groups.append(pairs)
        domain = next((item.domain for item in items if item.domain), "unspecified")
        bucket = by_domain.setdefault(domain, {key: [] for key in keys})
        for key, values in pairs.items():
            combined[key].extend(values)
            bucket[key].extend(values)
        per_manuscript.append({"manuscript_sha256": digest, "domain": domain, **result})

    annotator_pairs = _annotator_pairs(dataset)
    uncertainty = _uncertainty(groups, bins)
    domains = {
        domain: {
            "manuscripts": sum(1 for row in per_manuscript if row["domain"] == domain),
            "identity_accuracy": classification_report(values["identity"])["accuracy"]
            if values["identity"] else None,
            "support_accuracy": classification_report(values["support"])["accuracy"]
            if values["support"] else None,
            "relevance_macro_f1": classification_report(values["relevance"])["macro_f1"]
            if values["relevance"] else None,
            "triage_recall": binary_report(values["triage"])["recall"] if values["triage"] else None,
        }
        for domain, values in sorted(by_domain.items())
    }

    return {
        "dataset": dataset.summary(),
        "manuscripts_evaluated": len(per_manuscript),
        "manuscripts_missing_artifact": missing,
        "per_manuscript": per_manuscript,
        "aggregate": {
            "identity": classification_report(combined["identity"])
            if combined["identity"]
            else None,
            "support": classification_report(combined["support"])
            if combined["support"]
            else None,
            "extraction": classification_report(combined["extraction"])
            if combined["extraction"]
            else None,
            "triage": binary_report(combined["triage"]) if combined["triage"] else None,
            "relevance": classification_report(combined["relevance"])
            if combined["relevance"] else None,
            "relevance_detector": _relevance_detector(combined["relevance_scores"], bins,
                                                      [group.get("relevance_scores", []) for group in groups]),
            "self_citation": binary_report(combined["self_citation"])
            if combined["self_citation"] else None,
            "metadata": binary_report(combined["metadata"]) if combined["metadata"] else None,
            "calibration": (
                {
                    "samples": len(combined["confidence"]),
                    "signal": "identity rank_strength",
                    "brier_score": brier_score(combined["confidence"]),
                    "expected_calibration_error": expected_calibration_error(
                        combined["confidence"], bins=bins
                    ),
                    "maximum_calibration_error": maximum_calibration_error(
                        combined["confidence"], bins=bins
                    ),
                    "reliability": calibration_bins(combined["confidence"], bins=bins),
                }
                if combined["confidence"]
                else None
            ),
        },
        "uncertainty": uncertainty,
        "domains": domains,
        "annotator_agreement": annotator_pairs,
        "limitations": [
            "Metrics describe agreement with these annotators on these manuscripts.",
            "They do not establish that a proposal is scientifically correct.",
            "Rank strength is a rule-based signal; calibration here is the test of it, "
            "not a claim that it is a probability.",
            "A small or single-annotator dataset cannot support a generalization claim.",
        ],
    }


def _relevance_detector(scores: list[tuple[float, bool]], bins: int,
                        groups: list[list[tuple[float, bool]]] | None = None) -> dict | None:
    """How well RS_final separates irrelevant citations, and where to cut it.

    The recommended threshold is the F1-optimal cut-off on this dataset. It is
    a candidate for `BORDERLINE_THRESHOLD`, to be confirmed on held-out data —
    tuning and reporting on the same set overstates performance.
    """
    if not scores:
        return None
    from src.evaluation.metrics import auroc, cross_validated_threshold, threshold_sweep

    sweep = threshold_sweep(scores, lower_is_positive=True)
    relevance_as_probability = [(score / 100.0, not irrelevant) for score, irrelevant in scores]
    return {
        "samples": len(scores),
        "positives": sum(1 for _, irrelevant in scores if irrelevant),
        "auroc": auroc([(100.0 - score, irrelevant) for score, irrelevant in scores]),
        "recommended_threshold": sweep["best"],
        "cross_validated": cross_validated_threshold(groups or []),
        "calibration": {
            "signal": "RS_final / 100 as P(relevant)",
            "brier_score": brier_score(relevance_as_probability),
            "expected_calibration_error": expected_calibration_error(relevance_as_probability, bins=bins),
            "reliability": calibration_bins(relevance_as_probability, bins=bins),
        },
        "note": "Tune thresholds on one split and report on another.",
    }


def _uncertainty(groups: list[dict], bins: int) -> dict:
    """95% manuscript-level bootstrap intervals for the headline metrics."""
    from src.evaluation.metrics import bootstrap_interval

    def pooled(key, report):
        def statistic(sample):
            values = [pair for group in sample for pair in group.get(key, [])]
            return report(values) if values else None
        return statistic

    intervals = {
        "identity_accuracy": pooled("identity", lambda v: classification_report(v)["accuracy"]),
        "support_macro_f1": pooled("support", lambda v: classification_report(v)["macro_f1"]),
        "relevance_macro_f1": pooled("relevance", lambda v: classification_report(v)["macro_f1"]),
        "triage_recall": pooled("triage", lambda v: binary_report(v)["recall"]),
        "triage_precision": pooled("triage", lambda v: binary_report(v)["precision"]),
        "identity_ece": pooled("confidence", lambda v: expected_calibration_error(v, bins=bins)),
    }
    result = {}
    for name, statistic in intervals.items():
        if any(group.get(name.split("_")[0] if name != "identity_ece" else "confidence") for group in groups):
            result[name] = bootstrap_interval(groups, statistic)
    return result


def _annotator_pairs(dataset: EvaluationDataset) -> dict | None:
    """Report inter-annotator agreement when an item was labeled twice."""
    grouped: dict[tuple[str, str], list[LabeledItem]] = {}
    for item in dataset.items:
        grouped.setdefault((item.manuscript_sha256, item.reference_marker), []).append(item)
    pairs: list[tuple[str, str]] = []
    for entries in grouped.values():
        labeled = [item for item in entries if item.identity_label]
        if len(labeled) >= 2:
            pairs.append((labeled[0].identity_label, labeled[1].identity_label))  # type: ignore[arg-type]
    if not pairs:
        return None
    return {"signal": "identity_label", **agreement(pairs)}


__all__ = ["evaluate_audit", "evaluate_dataset"]
