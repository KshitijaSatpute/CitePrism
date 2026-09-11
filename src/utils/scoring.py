"""Shared score-state helpers.

Missing or invalid scores are unavailable, never zero. This distinction keeps
provider failures out of the highest-risk bucket and out of clean results.
"""

from __future__ import annotations

from typing import Any


def numeric_score(reference: dict[str, Any]) -> float | None:
    value = reference.get("RS_final")
    if value is None:
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return score if 0 <= score <= 100 else None


def is_flagged(reference: dict[str, Any], threshold: float) -> bool:
    score = numeric_score(reference)
    return score is not None and score < threshold


def is_clean(reference: dict[str, Any], threshold: float) -> bool:
    score = numeric_score(reference)
    return score is not None and score >= threshold
