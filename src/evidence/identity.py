"""Conservative work identity resolution; match strength is not a probability."""

from difflib import SequenceMatcher
import re

from src.evidence import RESOLVER_VERSION
from src.evidence.text import bibliographic_text, digest_json
from src.utils.reference_matching import normalize_doi


def surnames(authors):
    names = set()
    for author in authors or []:
        name = author if isinstance(author, str) else author.get("display_name", "")
        if "," in name:
            name = name.split(",", 1)[0]
        else:
            parts = re.findall(r"[^\W\d_]+", name, re.UNICODE)
            name = parts[-1] if parts else ""
        name = bibliographic_text(name)
        if len(name) > 1:
            names.add(name)
    return names


def candidate_metadata(work):
    authors = work.get("authors") or [a.get("author", {}) for a in work.get("authorships", [])]
    location = work.get("primary_location") or {}
    return {"id": work.get("id"), "doi": work.get("doi"), "title": work.get("title"),
            "year": work.get("publication_year", work.get("year")), "authors": authors,
            "url": location.get("landing_page_url") or work.get("url"),
            "venue": (location.get("source") or {}).get("display_name"),
            "is_retracted": work.get("is_retracted")}


def resolve_candidates(candidates, parsed):
    expected_title = bibliographic_text(parsed.get("title"))
    expected_doi = normalize_doi(parsed.get("doi")).strip()
    expected_authors = surnames(parsed.get("authors"))
    ranked, seen = [], set()
    for work in candidates[:10]:
        metadata = candidate_metadata(work)
        identity = metadata.get("id") or normalize_doi(metadata.get("doi")) or digest_json(metadata)
        if identity in seen:
            continue
        seen.add(identity)
        title = bibliographic_text(metadata.get("title"))
        similarity = SequenceMatcher(None, expected_title, title).ratio() if expected_title and title else 0
        doi = normalize_doi(metadata.get("doi")).strip()
        doi_match = bool(expected_doi and expected_doi == doi)
        doi_conflict = bool(expected_doi and doi and expected_doi != doi)
        names = surnames(metadata.get("authors"))
        overlap = len(names & expected_authors) / len(expected_authors) if expected_authors else None
        authors_conflict = bool(names and expected_authors and not (names & expected_authors))
        # Exact DOI still needs review when the supplied title/authors contradict it.
        conflicts = []
        if doi_conflict:
            conflicts.append("doi_conflict")
        if authors_conflict:
            conflicts.append("authors_conflict")
        if expected_title and similarity < .8:
            conflicts.append("title_conflict")
        same_year = parsed.get("year") is not None and parsed.get("year") == metadata.get("year")
        strong_authors = overlap is not None and overlap >= .5
        eligible = not conflicts and (doi_match or (similarity >= .9 and strong_authors))
        strength = (1.0 if doi_match else .7 * similarity + .25 * (overlap or 0) + .05 * same_year)
        ranked.append({"candidate_id": str(identity), "metadata": metadata,
                       "signals": {"title_similarity": round(similarity, 4), "author_overlap": overlap,
                                   "doi_exact": doi_match, "year_equal": same_year},
                       "conflicts": conflicts, "eligible": eligible, "rank_strength": round(strength, 4),
                       "selected": False})
    ranked.sort(key=lambda c: c["rank_strength"], reverse=True)
    eligible = [c for c in ranked if c["eligible"]]
    status, selected = "not_found" if not ranked else "unresolved", None
    if eligible:
        if len(eligible) > 1 and eligible[0]["rank_strength"] - eligible[1]["rank_strength"] < .05:
            status = "ambiguous"
        else:
            selected = eligible[0]
            selected["selected"] = True
            status = "resolved"
    differences = []
    if selected:
        for field in ("year", "doi"):
            left, right = parsed.get(field), selected["metadata"].get(field)
            if left and right and (normalize_doi(left) != normalize_doi(right) if field == "doi" else left != right):
                differences.append({"field": field, "as_cited": left, "candidate": right})
    return {"status": status, "resolver_version": RESOLVER_VERSION,
            "selected_candidate_id": selected["candidate_id"] if selected else None,
            "method": "doi_exact" if selected and selected["signals"]["doi_exact"] else "title_authors",
            "candidates": ranked, "metadata_differences": differences,
            "note": "Rule-based identity evidence; rank strength is not calibrated confidence."}
