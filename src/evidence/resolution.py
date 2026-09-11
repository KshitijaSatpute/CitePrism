"""Multi-provider identity resolution.

One provider's index is never complete. OpenAlex is strong on indexed journal
literature, Crossref is authoritative for registered DOIs, and Semantic Scholar
covers preprints and computer-science venues that the other two miss or index
late.

This module gathers candidates from the enabled providers, merges records that
describe the same work, and hands the merged set to the existing rule-based
ranker. Two consequences are deliberate:

* **Corroboration is recorded, not scored.** When several providers return the
  same DOI, that is written into the candidate as evidence a reviewer can read.
  It does not silently inflate a match strength, because three indexes copying
  one wrong record is not three pieces of evidence.
* **Disagreement is preserved.** Conflicting metadata across providers becomes a
  visible field rather than being resolved by majority vote.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from src.evidence.identity import resolve_candidates
from src.evidence.providers import ProviderFailure
from src.evidence.text import bibliographic_text
from src.utils.reference_matching import normalize_doi

logger = logging.getLogger(__name__)

PROVIDER_ORDER = ("openalex", "crossref", "semantic_scholar")


def merge_key(work: dict) -> str:
    """Group records that describe the same work.

    DOI is authoritative when present. Otherwise fall back to a normalized
    title plus year, which is conservative: a false split costs a duplicate
    candidate, while a false merge would hide a genuine alternative.
    """
    doi = normalize_doi(work.get("doi")).strip()
    if doi:
        return f"doi:{doi}"
    title = bibliographic_text(work.get("title"))
    year = work.get("publication_year") or work.get("year") or ""
    if title:
        return f"title:{title[:120]}:{year}"
    return f"id:{work.get('id')}"


def merge_records(groups: dict[str, list[tuple[str, dict]]]) -> list[dict]:
    """Combine each provider group into one candidate work."""
    merged: list[dict] = []
    for key, entries in groups.items():
        # Prefer the record from the highest-priority provider as the base.
        entries = sorted(
            entries,
            key=lambda pair: PROVIDER_ORDER.index(pair[0])
            if pair[0] in PROVIDER_ORDER
            else len(PROVIDER_ORDER),
        )
        base = dict(entries[0][1])
        providers = [provider for provider, _ in entries]
        disagreements: list[dict] = []

        for provider, record in entries[1:]:
            for field in ("title", "publication_year", "doi", "abstract"):
                incoming, existing = record.get(field), base.get(field)
                if incoming in (None, "", []):
                    continue
                if existing in (None, "", []):
                    base[field] = incoming
                    continue
                if field == "abstract":
                    # Keep the longest abstract; note the alternative source.
                    if len(str(incoming)) > len(str(existing)):
                        base["abstract"] = incoming
                        base["abstract_provider"] = provider
                    continue
                same = (
                    normalize_doi(incoming) == normalize_doi(existing)
                    if field == "doi"
                    else (
                        bibliographic_text(incoming) == bibliographic_text(existing)
                        if field == "title"
                        else incoming == existing
                    )
                )
                if not same:
                    disagreements.append(
                        {
                            "field": field,
                            "provider": provider,
                            "value": incoming,
                            "base_provider": providers[0],
                            "base_value": existing,
                        }
                    )
            if not base.get("authors") and record.get("authors"):
                base["authors"] = record["authors"]
            if not base.get("open_access_pdf_url") and record.get("open_access_pdf_url"):
                base["open_access_pdf_url"] = record["open_access_pdf_url"]
            if record.get("is_retracted") is True:
                base["is_retracted"] = True
            external = dict(base.get("external_ids") or {})
            external.update(record.get("external_ids") or {})
            if external:
                base["external_ids"] = external

        base["_merge_key"] = key
        base["_providers"] = providers
        base["_provider_count"] = len(set(providers))
        base["_provider_disagreements"] = disagreements
        merged.append(base)
    return merged


def gather_candidates(parsed: dict, clients: dict, *, openalex_candidates=None) -> dict:
    """Query every enabled provider and merge the results.

    A provider failure never aborts resolution: the outage is recorded and the
    remaining providers still contribute. That is what turns a transient
    Crossref 503 into a note rather than an unresolved reference.
    """
    groups: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    errors: list[dict] = []
    counts: dict[str, int] = {}

    for work in openalex_candidates or []:
        record = dict(work)
        record.setdefault("_provider", "openalex")
        groups[merge_key(record)].append(("openalex", record))
    counts["openalex"] = len(openalex_candidates or [])

    for name, client in clients.items():
        try:
            results = client.search(parsed) or []
        except ProviderFailure as error:
            logger.info("Resolution provider %s unavailable: %s", name, error.code)
            errors.append({"provider": name, "error_code": error.code})
            counts[name] = 0
            continue
        except Exception:  # noqa: BLE001 - a client bug must not fail the audit
            logger.exception("Resolution provider %s raised an unexpected error.", name)
            errors.append({"provider": name, "error_code": "provider_error"})
            counts[name] = 0
            continue
        counts[name] = len(results)
        for record in results:
            if record.get("title") or record.get("doi"):
                groups[merge_key(record)].append((name, record))

    return {
        "candidates": merge_records(groups),
        "provider_errors": errors,
        "provider_candidate_counts": counts,
    }


def resolve_with_providers(parsed: dict, clients: dict, *, openalex_candidates=None) -> dict:
    """Full multi-provider resolution using the existing ranking rules."""
    gathered = gather_candidates(parsed, clients, openalex_candidates=openalex_candidates)
    merged = gathered["candidates"]
    resolution = resolve_candidates(merged, parsed)

    by_key = {work.get("_merge_key"): work for work in merged}
    identity_to_key = {str(work.get("id") or work.get("_merge_key")): work.get("_merge_key")
                       for work in merged}

    for candidate in resolution.get("candidates", []):
        key = identity_to_key.get(candidate["candidate_id"])
        source = by_key.get(key, {})
        providers = source.get("_providers") or []
        candidate["providers"] = sorted(set(providers))
        candidate["provider_agreement"] = len(set(providers))
        candidate["provider_disagreements"] = source.get("_provider_disagreements") or []
        if source.get("open_access_pdf_url"):
            candidate["open_access_pdf_url"] = source["open_access_pdf_url"]
        if source.get("external_ids"):
            candidate["external_ids"] = source["external_ids"]

    resolution["resolution_strategy"] = "multi_provider_v1"
    resolution["providers_queried"] = sorted(gathered["provider_candidate_counts"])
    resolution["provider_candidate_counts"] = gathered["provider_candidate_counts"]
    resolution["provider_errors"] = gathered["provider_errors"]
    if gathered["provider_errors"] and resolution["status"] in {"not_found", "unresolved"}:
        # Do not report "no such work" when an index was simply unreachable.
        resolution["status"] = "provider_unavailable"
        resolution["error_code"] = gathered["provider_errors"][0]["error_code"]
    resolution["note"] = (
        "Candidates were merged across scholarly indexes. Provider agreement is "
        "recorded as evidence; it is not a calibrated confidence and several "
        "indexes can repeat one upstream error."
    )
    return resolution


def selected_abstract(resolution: dict, merged_candidates: list[dict]) -> dict | None:
    """Return the abstract belonging to the selected candidate, if any."""
    selected_id = resolution.get("selected_candidate_id")
    if not selected_id:
        return None
    for work in merged_candidates:
        if str(work.get("id") or work.get("_merge_key")) != str(selected_id):
            continue
        abstract = work.get("abstract")
        if not abstract:
            return None
        return {
            "text": abstract,
            "provider": work.get("abstract_provider") or work.get("_provider") or "unknown",
            "url": work.get("url"),
            "doi": work.get("doi"),
        }
    return None


__all__ = [
    "gather_candidates",
    "merge_key",
    "merge_records",
    "resolve_with_providers",
    "selected_abstract",
]
