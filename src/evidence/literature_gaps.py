"""Evidence-based discovery of possibly missing literature.

CitePrism V2.0 asks the system to "surface possibly missing key literature" and
names, as an extension, "retrieval-augmented verification of suggested
citations". Before this module the only mechanism was a language model asked to
name three papers — which can and does invent papers that do not exist.

Two independent mechanisms now exist, and both are grounded in real records:

1. **Co-citation analysis (retrieval-first).** OpenAlex records which works each
   cited source itself cites. A work that several of the manuscript's own
   references build on, but that the manuscript never cites, is a concrete,
   checkable gap: "cited by 5 of your references". No model is involved, so
   nothing can be hallucinated, and every candidate is a resolved record.

2. **Verification of model suggestions (retrieval-augmented).** Each search lead
   from the language model is resolved against the scholarly indexes with the
   same identity rules used for the bibliography. A lead is shown as *verified*
   only when a real record matches it; leads with no matching record are marked
   as such instead of being presented as literature.

Neither mechanism claims the manuscript *must* cite anything. A frequently
co-cited work may be irrelevant to this manuscript's contribution; deciding
that is the reviewer's job.
"""

from __future__ import annotations

import logging
import math
import re
from collections import defaultdict

from src.evidence.providers import ProviderFailure, utc_now
from src.evidence.text import bibliographic_text, digest_json
from src.utils.reference_matching import normalize_doi

logger = logging.getLogger(__name__)

GAP_VERSION = "literature-gaps-v1"
OPENALEX_ID = re.compile(r"(W\d+)$")
FETCH_FIELDS = "id,doi,title,publication_year,authorships,cited_by_count,primary_location,type"


def short_id(value) -> str | None:
    match = OPENALEX_ID.search(str(value or "").strip().rstrip("/"))
    return match.group(1) if match else None


def cited_identity(scored_references: list[dict]) -> dict:
    """Everything that identifies a work the manuscript already cites."""
    openalex, dois, titles = set(), set(), set()
    for reference in scored_references:
        external = reference.get("external_metadata") or {}
        parsed = (reference.get("original_data") or {}).get("parsed") or {}
        for value in (external.get("id"), (external.get("graph") or {}).get("openalex_id")):
            identifier = short_id(value)
            if identifier:
                openalex.add(identifier)
        for value in (external.get("doi"), parsed.get("doi")):
            doi = normalize_doi(value).strip()
            if doi:
                dois.add(doi)
        for value in (external.get("title"), parsed.get("title")):
            title = bibliographic_text(value)
            if len(title) > 15:
                titles.add(title)
    return {"openalex": openalex, "dois": dois, "titles": titles}


def cocitation_candidates(scored_references: list[dict], *, min_support: int | None = None,
                          limit: int = 15) -> dict:
    """Works cited by several of the manuscript's references but not by the manuscript."""
    cited = cited_identity(scored_references)
    support: dict[str, list[str]] = defaultdict(list)
    with_graph = 0
    for reference in scored_references:
        if (reference.get("resolution") or {}).get("status") != "resolved":
            continue
        graph = (reference.get("external_metadata") or {}).get("graph") or {}
        works = graph.get("referenced_works") or []
        if not works:
            continue
        with_graph += 1
        marker = str(reference.get("ref_id"))
        for value in set(works):
            identifier = short_id(value)
            if identifier and identifier not in cited["openalex"]:
                support[identifier].append(marker)
    threshold = min_support if min_support is not None else max(2, math.ceil(0.08 * with_graph))
    ranked = sorted(
        ((identifier, markers) for identifier, markers in support.items() if len(markers) >= threshold),
        key=lambda item: (-len(item[1]), item[0]),
    )
    return {
        "references_with_graph": with_graph,
        "min_support": threshold,
        "candidates": [
            {"openalex_id": identifier, "support_count": len(markers),
             "supporting_references": sorted(markers, key=_marker_order)}
            for identifier, markers in ranked[:limit]
        ],
        "cited": cited,
    }


def _marker_order(marker: str):
    digits = re.sub(r"\D", "", marker)
    return (int(digits) if digits else 10**9, marker)


def _authors(work: dict) -> list[str]:
    return [
        str((item.get("author") or {}).get("display_name"))
        for item in (work.get("authorships") or [])[:6]
        if (item.get("author") or {}).get("display_name")
    ]


def parse_suggestion(suggestion: dict) -> dict:
    """Turn a free-text model suggestion into the parsed-reference shape."""
    authors_text = str(suggestion.get("authors") or "")
    authors = [part.strip() for part in re.split(r",|;|\band\b|&", authors_text) if part.strip()]
    authors = [name for name in authors if name.casefold() not in {"et al", "et al.", "unknown"}]
    year = None
    match = re.search(r"\b(1[89]\d\d|20\d\d)\b", str(suggestion.get("year") or ""))
    if match:
        year = int(match.group(1))
    return {"title": str(suggestion.get("title") or "").strip() or None,
            "authors": authors[:6], "year": year, "doi": suggestion.get("doi")}


class LiteratureGapAnalyzer:
    """Run both mechanisms against a scored artifact."""

    def __init__(self, config, db, *, transport=None, clients=None):
        self.config = config
        self.db = db
        self.transport = transport
        self.clients = clients

    # -- network helpers ------------------------------------------------
    def _cached(self, key):
        if self.db is None:
            return None
        try:
            return self.db.get_cached_response("gap", key)
        except Exception:  # noqa: BLE001 - the cache is an optimization
            return None

    def _store(self, key, value):
        if self.db is None:
            return
        try:
            self.db.cache_api_response("gap", key, value)
        except Exception:  # noqa: BLE001
            logger.debug("Gap-analysis cache write failed.")

    def fetch_works(self, identifiers: list[str]) -> dict[str, dict]:
        """Metadata for OpenAlex works, 50 per request, cached."""
        found: dict[str, dict] = {}
        for start in range(0, len(identifiers), 50):
            batch = identifiers[start:start + 50]
            key = digest_json({"version": GAP_VERSION, "works": batch})
            cached = self._cached(key)
            if cached is None:
                params = {"filter": "openalex:" + "|".join(batch), "per-page": 50, "select": FETCH_FIELDS}
                try:
                    document = self.transport._get_json("openalex", "https://api.openalex.org/works", params)
                except ProviderFailure as error:
                    if error.code != "invalid_request":
                        raise
                    params.pop("select")
                    document = self.transport._get_json("openalex", "https://api.openalex.org/works", params)
                cached = {"results": document.get("results", []), "observed_at": utc_now()}
                self._store(key, cached)
            for work in cached.get("results", []):
                identifier = short_id(work.get("id"))
                if identifier:
                    found[identifier] = work
        return found

    # -- mechanism 1 ----------------------------------------------------
    def cocitation(self, scored_references: list[dict]) -> dict:
        gathered = cocitation_candidates(
            scored_references,
            min_support=getattr(self.config, "GAP_MIN_SUPPORT", None),
            limit=int(getattr(self.config, "GAP_MAX_CANDIDATES", 15)),
        )
        cited = gathered.pop("cited")
        candidates = gathered["candidates"]
        if not candidates:
            return {**gathered, "status": "completed" if gathered["references_with_graph"] else
                    "no_citation_graph", "candidates": []}
        try:
            works = self.fetch_works([item["openalex_id"] for item in candidates])
        except ProviderFailure as error:
            return {**gathered, "status": "provider_unavailable", "error_code": error.code}
        enriched = []
        for item in candidates:
            work = works.get(item["openalex_id"])
            if not work:
                continue
            doi = normalize_doi(work.get("doi")).strip() or None
            title = work.get("title")
            if (doi and doi in cited["dois"]) or bibliographic_text(title) in cited["titles"]:
                continue  # cited under a different record; not a gap
            enriched.append({
                **item,
                "title": title,
                "year": work.get("publication_year"),
                "doi": doi,
                "authors": _authors(work),
                "venue": ((work.get("primary_location") or {}).get("source") or {}).get("display_name"),
                "cited_by_count": work.get("cited_by_count"),
                "type": work.get("type"),
                "url": f"https://openalex.org/{item['openalex_id']}",
            })
        return {**gathered, "status": "completed", "candidates": enriched}

    # -- mechanism 2 ----------------------------------------------------
    def verify_suggestions(self, suggestions: list[dict], scored_references: list[dict]) -> list[dict]:
        if not suggestions:
            return []
        from src.evidence.resolution import resolve_with_providers

        cited = cited_identity(scored_references)
        verified = []
        for index, suggestion in enumerate(suggestions):
            if not isinstance(suggestion, dict) or not suggestion.get("title"):
                continue
            parsed = parse_suggestion(suggestion)
            record = {
                "id": f"candidate-{index + 1}",
                "title": parsed["title"],
                "authors": str(suggestion.get("authors") or ""),
                "year": suggestion.get("year"),
                "rationale": str(suggestion.get("rationale") or ""),
                "origin": "language_model",
            }
            key = digest_json({"version": GAP_VERSION, "verify": parsed})
            outcome = self._cached(key)
            if outcome is None:
                try:
                    openalex = []
                    if self.transport is not None and parsed["title"]:
                        openalex = self.transport._get_json(
                            "openalex", "https://api.openalex.org/works",
                            {"search": parsed["title"][:300], "per-page": 10},
                        ).get("results", [])
                    resolution = resolve_with_providers(parsed, self.clients or {},
                                                        openalex_candidates=openalex)
                    selected = next((c for c in resolution["candidates"] if c.get("selected")), None)
                    outcome = {"status": resolution["status"],
                               "match": selected["metadata"] if selected else None,
                               "signals": selected["signals"] if selected else None,
                               "candidates_considered": len(resolution["candidates"])}
                    self._store(key, outcome)
                except ProviderFailure as error:
                    outcome = {"status": "provider_unavailable", "error_code": error.code}
            status = outcome.get("status")
            match = outcome.get("match") or {}
            match_doi = normalize_doi(match.get("doi")).strip() or None
            already = bool(
                (match_doi and match_doi in cited["dois"])
                or short_id(match.get("id")) in cited["openalex"]
                or bibliographic_text(parsed["title"]) in cited["titles"]
            )
            record.update({
                "verification_status": {
                    "resolved": "verified",
                    "ambiguous": "ambiguous_match",
                    "not_found": "no_matching_record",
                    "unresolved": "no_matching_record",
                    "provider_unavailable": "verification_unavailable",
                }.get(status, "verification_unavailable"),
                "matched_record": {
                    "title": match.get("title"), "doi": match_doi, "year": match.get("year"),
                    "id": match.get("id"), "venue": match.get("venue"),
                } if match else None,
                "match_signals": outcome.get("signals"),
                "already_cited": already,
            })
            verified.append(record)
        return verified

    # -- both -----------------------------------------------------------
    def analyze(self, artifact: dict) -> dict:
        references = artifact.get("scored_references", [])
        if getattr(self.config, "OFFLINE_MODE", False) or self.transport is None:
            return {"method_version": GAP_VERSION, "status": "offline_mode", "generated_at": utc_now(),
                    "cocitation": {"status": "offline_mode", "candidates": []},
                    "suggestions": [], "limitations": LIMITATIONS}
        try:
            cocitation = self.cocitation(references)
        except Exception as error:  # noqa: BLE001 - gap analysis is never load bearing
            logger.exception("Co-citation analysis failed.")
            cocitation = {"status": "failed", "error_code": type(error).__name__.lower(), "candidates": []}
        suggestions = []
        if artifact.get("missing_citations"):
            try:
                suggestions = self.verify_suggestions(artifact["missing_citations"], references)
            except Exception:  # noqa: BLE001
                logger.exception("Suggestion verification failed.")
        verified = sum(item["verification_status"] == "verified" for item in suggestions)
        return {
            "method_version": GAP_VERSION,
            "status": "completed",
            "generated_at": utc_now(),
            "cocitation": cocitation,
            "suggestions": suggestions,
            "counts": {
                "cocitation_candidates": len(cocitation.get("candidates", [])),
                "model_suggestions": len(suggestions),
                "verified_suggestions": verified,
                "unmatched_suggestions": sum(
                    item["verification_status"] == "no_matching_record" for item in suggestions
                ),
            },
            "limitations": LIMITATIONS,
        }


LIMITATIONS = [
    "Co-citation candidates are works that several cited sources themselves cite. "
    "Frequency is evidence of importance to the field, not of relevance to this manuscript.",
    "Coverage depends on OpenAlex reference lists, which are incomplete for some "
    "publishers, older works and non-English literature.",
    "A model suggestion marked 'no matching record' may be a mistitled real work "
    "or a work that does not exist; it must not be cited without a source.",
    "Nothing here states that the manuscript is required to cite any work.",
]


__all__ = [
    "GAP_VERSION",
    "LIMITATIONS",
    "LiteratureGapAnalyzer",
    "cited_identity",
    "cocitation_candidates",
    "parse_suggestion",
    "short_id",
]
