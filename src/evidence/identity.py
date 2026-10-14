"""Conservative work identity resolution; match strength is not a probability."""

from difflib import SequenceMatcher
import re

from src.evidence import RESOLVER_VERSION
from src.evidence.text import bibliographic_text, digest_json
from src.utils.reference_matching import normalize_doi


def surnames(authors):
    """Surnames for matching, from the one shared name parser.

    "Smith JA" and "He K" (Vancouver style) are Smith and He, not "ja" and nothing;
    a separate parser here once rejected exact DOI matches as author conflicts.
    """
    from src.evidence.self_citation import person_key

    names = set()
    for author in authors or []:
        key = person_key(author)
        if key:
            name = bibliographic_text(key[0])
            if len(name) > 1:
                names.add(name)
    return names


def surname_sets(authors):
    """One set of possible surnames per author (see `surname_variants`)."""
    from src.evidence.self_citation import surname_variants

    sets = []
    for author in authors or []:
        variants = {bibliographic_text(variant) for variant in surname_variants(author)}
        variants = {variant for variant in variants if len(variant) > 1}
        if variants:
            sets.append(variants)
    return sets


def candidate_metadata(work):
    authors = work.get("authors") or [a.get("author", {}) for a in work.get("authorships", [])]
    location = work.get("primary_location") or {}
    return {"id": work.get("id"), "doi": work.get("doi"), "title": work.get("title"),
            "year": work.get("publication_year", work.get("year")), "authors": authors,
            "url": location.get("landing_page_url") or work.get("url"),
            "venue": (location.get("source") or {}).get("display_name"),
            "is_retracted": work.get("is_retracted")}


def _main_title(text):
    """The title before its subtitle, in bibliographic_text form."""
    head = re.split(r"\s*[:.?!]\s+|\s+[-–—]\s+", text or "", maxsplit=1)[0]
    return bibliographic_text(head)


def title_similarity(expected, found):
    """Best agreement between two titles, tolerating an added or dropped subtitle.

    Indexes often store "Main title" and "Subtitle" separately, and printed
    references often join them with a full stop instead of a colon. Comparing
    only full strings made a correct DOI match look like a title conflict.
    """
    left, right = bibliographic_text(expected), bibliographic_text(found)
    if not left or not right:
        return 0
    scores = [SequenceMatcher(None, left, right).ratio()]
    main_left, main_right = _main_title(expected), _main_title(found)
    left_has_subtitle, right_has_subtitle = main_left != left, main_right != right
    # A subtitle is ignored only when one side has none. Two different subtitles
    # under one main title ("Medienkompetenz – Begrifflichkeit…" and
    # "Medienkompetenz: theoretisch erschließend…") are two different works.
    if left_has_subtitle and right_has_subtitle:
        return scores[0]
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) >= 12 and longer.startswith(shorter):
        scores.append(.95)
    for a, b in ((main_left, right), (left, main_right)):
        if a and b and min(len(a), len(b)) >= 12:
            scores.append(SequenceMatcher(None, a, b).ratio())
    return max(scores)


def resolve_candidates(candidates, parsed):
    expected_title = bibliographic_text(parsed.get("title"))
    expected_doi = normalize_doi(parsed.get("doi")).strip()
    expected_authors = surname_sets(parsed.get("authors"))
    ranked, seen = [], set()
    for work in candidates[:10]:
        metadata = candidate_metadata(work)
        identity = metadata.get("id") or normalize_doi(metadata.get("doi")) or digest_json(metadata)
        if identity in seen:
            continue
        seen.add(identity)
        title = bibliographic_text(metadata.get("title"))
        similarity = title_similarity(parsed.get("title"), metadata.get("title")) if expected_title and title else 0
        venue_similarity = (title_similarity(parsed.get("title"), metadata.get("venue"))
                            if expected_title and metadata.get("venue") else 0)
        doi = normalize_doi(metadata.get("doi")).strip()
        doi_match = bool(expected_doi and expected_doi == doi)
        doi_conflict = bool(expected_doi and doi and expected_doi != doi)
        found = surname_sets(metadata.get("authors"))
        on_record = set().union(*found) if found else set()
        shared = sum(bool(variants & on_record) for variants in expected_authors)
        overlap = shared / len(expected_authors) if expected_authors else None
        authors_conflict = bool(found and expected_authors and not shared)
        # Exact DOI still needs review when the supplied title/authors contradict it.
        conflicts = []
        if doi_conflict:
            conflicts.append("doi_conflict")
        if authors_conflict:
            conflicts.append("authors_conflict")
        # A printed book whose DOI is one of its chapters: the record's container
        # is the cited book. Accepted, and reported as a DOI detail to correct.
        chapter_of_cited_book = bool(doi_match and similarity < .8 and venue_similarity >= .8
                                     and not authors_conflict)
        if expected_title and similarity < .8 and not chapter_of_cited_book:
            conflicts.append("title_conflict")
        same_year = parsed.get("year") is not None and parsed.get("year") == metadata.get("year")
        strong_authors = overlap is not None and overlap >= .5
        eligible = not conflicts and (doi_match or (similarity >= .9 and strong_authors))
        strength = (1.0 if doi_match else .7 * similarity + .25 * (overlap or 0) + .05 * same_year)
        ranked.append({"candidate_id": str(identity), "metadata": metadata,
                       "signals": {"title_similarity": round(similarity, 4), "author_overlap": overlap,
                                   "doi_exact": doi_match, "year_equal": same_year,
                                   "chapter_of_cited_book": chapter_of_cited_book},
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
