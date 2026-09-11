"""
Reference Risk Summary
======================

Aggregates existing CitePrism audit results into an explainable
Low / Medium / High editorial reference-risk classification.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional


class ReferenceRiskSummarizer:
    """Generate an explainable reference-risk summary."""

    def __init__(
        self,
        relevant_threshold: float = 70.0,
        borderline_threshold: float = 40.0,
        old_reference_years: int = 15,
    ):
        self.relevant_threshold = relevant_threshold
        self.borderline_threshold = borderline_threshold
        self.old_reference_years = old_reference_years

    @staticmethod
    def _get_score(item):
        value = item.get("RS_final")

        if value is None:
            return None

        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _get_reference(item):
        return item.get("original_data", {}).get("parsed", {})

    @staticmethod
    def _is_metadata_mismatch(item: Dict[str, Any]) -> bool:
        """Detect metadata mismatch flags from current and legacy schemas."""
        mismatch_keys = [
            "metadata_mismatch",
            "title_mismatch",
            "author_mismatch",
            "year_mismatch",
            "doi_mismatch",
        ]

        if any(bool(item.get(key)) for key in mismatch_keys):
            return True

        consistency = str(item.get("consistency_status", "")).lower()
        if "mismatch" in consistency:
            return True

        flags = item.get("quality_flags") or []
        if isinstance(flags, str):
            flags = [flags]
        return any("metadata mismatch" in str(flag).lower() for flag in flags)

    @staticmethod
    def _is_self_citation(item):
        return (
            item.get("self_citation", {})
                .get("is_self_cite", False)
        )

    def summarize(
        self,
        scored_items: List[Dict[str, Any]],
        current_year: int,
    ) -> Dict[str, Any]:
        """
        Produce the final manuscript-level reference risk summary.
        """

        counts = Counter(
            {
                "total_references": len(scored_items),
                "weak_or_irrelevant": 0,
                "borderline": 0,
                "self_citations": 0,
                "missing_doi": 0,
                "missing_abstract": 0,
                "outdated_references": 0,
                "metadata_mismatches": 0,
            }
        )

        issues: List[Dict[str, Any]] = []

        for index, item in enumerate(scored_items, start=1):
            reference = self._get_reference(item)
            score = self._get_score(item)

            ref_id = (
                item.get("ref_id")
                or reference.get("ref_id")
                or reference.get("id")
                or f"Reference {index}"
            )

            title = reference.get("title") or "Unknown title"
            issue_types = []

            # Prefer the persisted label. Fall back to the configured numeric
            # thresholds for older artifacts that predate labels.
            label = str(item.get("label", "")).strip().lower()

            if label in {"irrelevant", "weak", "weak_or_irrelevant"}:
                counts["weak_or_irrelevant"] += 1
                issue_types.append("weak_or_irrelevant")

            elif label == "borderline":
                counts["borderline"] += 1
                issue_types.append("borderline")

            elif score is not None:
                if score < self.borderline_threshold:
                    counts["weak_or_irrelevant"] += 1
                    issue_types.append("weak_or_irrelevant")
                elif score < self.relevant_threshold:
                    counts["borderline"] += 1
                    issue_types.append("borderline")

            # Keep the numeric score only for display
            score = self._get_score(item)

            # Self-citation
            if self._is_self_citation(item):
                counts["self_citations"] += 1
                issue_types.append("self_citation")

            external_metadata = item.get("external_metadata", {})

            # DOI
            doi = external_metadata.get("doi") or reference.get("doi")

            if not doi:
                counts["missing_doi"] += 1
                issue_types.append("missing_doi")

            # Abstract
            abstract = external_metadata.get("abstract")

            if not abstract:
                counts["missing_abstract"] += 1
                issue_types.append("missing_abstract")

            # Age
            year = external_metadata.get("year") or reference.get("year")

            try:
                if year and current_year - int(year) > self.old_reference_years:
                    counts["outdated_references"] += 1
                    issue_types.append("outdated_reference")
            except (TypeError, ValueError):
                pass

            # Metadata consistency
            if self._is_metadata_mismatch(item):
                counts["metadata_mismatches"] += 1
                issue_types.append("metadata_mismatch")

            if issue_types:
                issues.append(
                    {
                        "ref_id": ref_id,
                        "title": title,
                        "score": score,
                        "issues": issue_types,
                    }
                )

        if not scored_items:
            return {
                "risk_level": "Unknown",
                "risk_score": None,
                "message": "No scored references are available; review priority cannot be calculated.",
                "counts": dict(counts),
                "rates": {},
                "coverage": {"scored_references": 0, "total_references": 0},
                "top_issues": [],
                "flagged_references": [],
            }

        total = counts["total_references"]
        scored_count = sum(
            1 for item in scored_items if self._get_score(item) is not None
        )
        rates = {
            "weak_or_irrelevant_rate": round(
                counts["weak_or_irrelevant"] / total, 3
            ),
            "borderline_rate": round(counts["borderline"] / total, 3),
            "missing_doi_rate": round(counts["missing_doi"] / total, 3),
            "missing_abstract_rate": round(counts["missing_abstract"] / total, 3),
            "outdated_reference_rate": round(
                counts["outdated_references"] / total, 3
            ),
            "metadata_mismatch_rate": round(
                counts["metadata_mismatches"] / total, 3
            ),
        }
        coverage = {
            "scored_references": scored_count,
            "total_references": total,
            "complete": scored_count == total,
        }

        if scored_count < total:
            return {
                "risk_level": "Incomplete",
                "risk_score": None,
                "message": (
                    "One or more references lack a valid score; review priority "
                    "was not calculated."
                ),
                "classification": "editorial_review_priority",
                "counts": dict(counts),
                "rates": rates,
                "coverage": coverage,
                "methodology": {
                    "version": "review-priority-v2",
                    "calibrated": False,
                    "intended_use": "prototype editorial triage only",
                },
                "top_issues": self._top_issues(counts),
                "flagged_references": issues,
            }

        risk_score = self._calculate_risk_score(counts, total)
        risk_level = self._risk_level(risk_score)
        message = self._risk_message(risk_level)

        return {
            "risk_level": risk_level,
            "risk_score": round(risk_score, 2),
            "message": message,
            "classification": "editorial_review_priority",
            "counts": dict(counts),
            "rates": rates,
            "coverage": coverage,
            "methodology": {
                "version": "review-priority-v2",
                "calibrated": False,
                "intended_use": "prototype editorial triage only",
            },
            "top_issues": self._top_issues(counts),
            "flagged_references": issues,
        }

    @staticmethod
    def _calculate_risk_score(counts: Counter, total: int) -> float:
        """
        Weighted, explainable risk score from 0 to 100.

        Relevance and metadata problems receive higher weights.
        Missing abstracts and older references receive lower weights.
        """

        rates = {
            key: counts[key] / total
            for key in [
                "weak_or_irrelevant",
                "borderline",
                "metadata_mismatches",
            ]
        }

        risk_score = (
            rates["weak_or_irrelevant"] * 35
            + rates["borderline"] * 15
            + rates["metadata_mismatches"] * 25
        )

        return min(risk_score, 100.0)

    @staticmethod
    def _risk_level(risk_score: float) -> str:
        if risk_score < 15:
            return "Low"

        if risk_score < 35:
            return "Medium"

        return "High"

    @staticmethod
    def _risk_message(risk_level: str) -> str:
        messages = {
            "Low": "Automated signals indicate a lower review priority; human review is still required.",
            "Medium": "Automated signals indicate a moderate review priority.",
            "High": "Automated signals indicate a higher review priority; they do not establish an error or misconduct.",
        }

        return messages[risk_level]

    @staticmethod
    def _top_issues(counts: Counter) -> List[Dict[str, Any]]:
        labels = {
            "weak_or_irrelevant": "Weak or irrelevant citations",
            "borderline": "Borderline citations",
            "self_citations": "Author-name overlap (descriptive only)",
            "missing_doi": "Missing DOI",
            "missing_abstract": "Missing abstract",
            "outdated_references": "Old or potentially outdated references",
            "metadata_mismatches": "Metadata mismatches",
        }

        ranked = [
            {
                "issue": labels[key],
                "count": counts[key],
            }
            for key in labels
            if counts[key] > 0
        ]

        return sorted(
            ranked,
            key=lambda item: item["count"],
            reverse=True,
        )[:5]
