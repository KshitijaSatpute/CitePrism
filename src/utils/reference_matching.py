"""Deterministic candidate selection for external reference metadata."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any


def normalize_doi(value: str | None) -> str:
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value or "", flags=re.I).lower()


def select_best_candidate(
    candidates: list[dict[str, Any]],
    parsed: dict[str, Any],
    minimum_title_similarity: float,
) -> dict[str, Any] | None:
    """Select a defensible metadata candidate or abstain.

    DOI equality wins. Otherwise title similarity is primary, with only a
    small year-consistency bonus. A first search result is never accepted just
    because it exists.
    """
    parsed_title = str(parsed.get("title") or "").strip().lower()
    parsed_doi = normalize_doi(parsed.get("doi"))
    parsed_year = parsed.get("year")
    ranked = []

    for candidate in candidates[:5]:
        candidate_doi = normalize_doi(candidate.get("doi"))
        title = str(candidate.get("title") or "").strip().lower()
        title_similarity = SequenceMatcher(None, parsed_title, title).ratio()
        doi_match = bool(parsed_doi and candidate_doi == parsed_doi)
        try:
            year_match = (
                parsed_year is not None
                and candidate.get("publication_year") is not None
                and abs(int(parsed_year) - int(candidate["publication_year"])) <= 1
            )
        except (TypeError, ValueError):
            year_match = False
        confidence = (
            1.0
            if doi_match
            else min(1.0, title_similarity + (0.03 if year_match else 0))
        )
        ranked.append((confidence, title_similarity, doi_match, candidate))

    if not ranked:
        return None

    confidence, title_similarity, doi_match, candidate = max(
        ranked, key=lambda item: (item[0], item[1])
    )
    if not doi_match and title_similarity < minimum_title_similarity:
        return None

    selected = dict(candidate)
    selected["_match_confidence"] = round(confidence, 4)
    selected["_match_method"] = "doi_exact" if doi_match else "title_year_ranked"
    return selected
