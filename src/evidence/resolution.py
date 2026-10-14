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

from src.evidence.identity import candidate_metadata, resolve_candidates
from src.evidence.providers import ProviderFailure
from src.evidence.text import bibliographic_text, digest_json
from src.utils.reference_matching import normalize_doi

logger = logging.getLogger(__name__)

PROVIDER_ORDER = ("openalex", "crossref", "semantic_scholar")


def candidate_identity(work: dict) -> str:
    """The candidate id `resolve_candidates` assigns to this work."""
    metadata = candidate_metadata(work)
    return str(metadata.get("id") or normalize_doi(metadata.get("doi")) or digest_json(metadata))


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


# Indexes that each cover one field: dblp (computer science), Europe PMC (life
# sciences), DataCite (datasets, software, preprints). They are asked only after the
# general indexes (OpenAlex, Crossref, Semantic Scholar) could not settle a reference.
SPECIALIST_INDEXES = frozenset({"dblp", "europe_pmc", "datacite"})


def gather_candidates(parsed: dict, clients: dict, *, openalex_candidates=None, prior: dict | None = None) -> dict:
    """Query every enabled provider and merge the results.

    A provider failure never aborts resolution: the outage is recorded and the
    remaining providers still contribute. That is what turns a transient
    Crossref 503 into a note rather than an unresolved reference.
    """
    groups: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    errors: list[dict] = []
    counts: dict[str, int] = {}
    if prior:
        # Extending an earlier round (supplementary indexes): keep its records,
        # outages and counts so the merge and the DOI check see everything.
        for key, members in (prior.get("_groups") or {}).items():
            groups[key].extend(members)
        errors.extend(prior.get("provider_errors") or [])
        counts.update(prior.get("provider_candidate_counts") or {})
    else:
        for work in openalex_candidates or []:
            record = dict(work)
            record.setdefault("_provider", "openalex")
            groups[merge_key(record)].append(("openalex", record))
        if openalex_candidates is not None:     # None: OpenAlex is not enabled and was not asked
            counts["openalex"] = len(openalex_candidates)

    for name, client in clients.items():
        try:
            results = client.search(parsed) or []
        except ProviderFailure as error:
            logger.info("Resolution provider %s unavailable: %s", name, error.code)
            errors.append({"provider": name, "error_code": error.code,
                           **({"http_status": error.status_code} if error.status_code else {})})
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
        "_groups": groups,
    }


def resolve_with_providers(
    parsed: dict, clients: dict, *, openalex_candidates=None, return_merged: bool = False,
    supplementary: dict | None = None, registry=None,
):
    """Full multi-provider resolution using the existing ranking rules.

    With `return_merged=True` the result is `(resolution, merged_by_candidate)`,
    where `merged_by_candidate` maps each ranked candidate id to the merged
    provider record behind it. The resolution itself stores only compact
    candidate metadata; the merged record carries abstracts and citation-graph
    fields the caller needs for the selected work.
    """
    gathered = gather_candidates(parsed, clients, openalex_candidates=openalex_candidates)
    merged = gathered["candidates"]
    resolution = resolve_candidates(merged, parsed)
    supplementary_queried: list[str] = []
    if supplementary and resolution.get("status") != "resolved":
        # Specialist indexes (dblp, Europe PMC, DataCite) are slower and narrower;
        # asking them only for references the primary indexes could not settle
        # keeps ordinary audits fast while closing the coverage gaps.
        gathered = gather_candidates(parsed, supplementary, prior=gathered)
        merged = gathered["candidates"]
        resolution = resolve_candidates(merged, parsed)
        supplementary_queried = sorted(supplementary)

    by_key = {work.get("_merge_key"): work for work in merged}
    # Must use the identity rule `resolve_candidates` uses, or a merged record
    # without an `id` could never be matched back to its ranked candidate.
    identity_to_key = {candidate_identity(work): work.get("_merge_key") for work in merged}

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
    resolution["supplementary_providers_queried"] = supplementary_queried
    doi_record = next((c for c in resolution.get("candidates", []) if c["signals"].get("doi_exact")), None)
    if doi_record is not None and not doi_record.get("selected"):
        # The printed DOI exists but names a different work. That is a fact about
        # the manuscript, so an unreachable index elsewhere must not hide it.
        resolution["printed_doi_record"] = {
            "title": doi_record["metadata"].get("title"), "year": doi_record["metadata"].get("year"),
            "conflicts": doi_record.get("conflicts", []),
        }
    if (gathered["provider_errors"] and resolution["status"] in {"not_found", "unresolved"}
            and doi_record is None):
        failed = {item.get("provider") for item in gathered["provider_errors"]}
        if failed <= SPECIALIST_INDEXES:
            # Every general index answered; only a specialist one (dblp covers computer
            # science only) did not. The search is finished but incomplete, and the
            # finding names the index that did not answer instead of leaving the
            # reference unchecked — a dblp outage left three engineering references
            # "not finished" in a real audit although five databases had answered.
            resolution["search_incomplete"] = sorted(failed)
        else:
            # Do not report "no such work" when a general index was simply unreachable.
            resolution["status"] = "provider_unavailable"
            resolution["error_code"] = gathered["provider_errors"][0]["error_code"]
    resolution["note"] = (
        "Candidates were merged across scholarly indexes. Provider agreement is "
        "recorded as evidence; it is not a calibrated confidence and several "
        "indexes can repeat one upstream error."
    )
    printed_doi = normalize_doi(parsed.get("doi")).strip()
    if printed_doi:
        status = printed_doi_status(printed_doi, merged, gathered)
        failed = {item.get("provider") for item in gathered.get("provider_errors", [])}
        # doi.org answers for every registration agency, so it settles the DOI even when only a
        # specialist index (DataCite) was unreachable; a general-index outage still leaves it unknown.
        ask_registry = status == "not_indexed" or (status == "unknown" and failed and failed <= SPECIALIST_INDEXES)
        if ask_registry and registry is not None:
            # Only doi.org can say a DOI was never registered: it answers for every
            # registration agency, not just the ones the indexes cover.
            try:
                answer = registry.registered(printed_doi)
            except Exception:  # noqa: BLE001 - never fail resolution over this check
                logger.exception("doi.org check raised; registration left unknown.")
                answer = None
            if answer is True:
                status = "registered"
                resolution["printed_doi_registry"] = "doi.org"
            elif answer is False:
                status = "unregistered"
                resolution["printed_doi_registry"] = "doi.org"
        datacite = (supplementary or {}).get("datacite")
        if status == "not_indexed" and datacite is not None and "datacite" not in gathered["provider_candidate_counts"]:
            # doi.org could not be asked: arXiv (10.48550) and Zenodo (10.5281) DOIs
            # are at least found in DataCite, which Crossref does not cover.
            try:
                hits = datacite.search({"doi": printed_doi}) or []
                if any(normalize_doi(hit.get("doi")) == printed_doi for hit in hits):
                    status = "registered"
                    resolution["printed_doi_registry"] = "datacite"
            except ProviderFailure:
                pass
            except Exception:  # noqa: BLE001 - never fail resolution over this check
                logger.exception("DataCite DOI check raised; registration left unknown.")
        resolution["printed_doi_status"] = status
    if not return_merged:
        return resolution
    merged_by_candidate = {
        candidate["candidate_id"]: by_key.get(identity_to_key.get(candidate["candidate_id"]), {})
        for candidate in resolution.get("candidates", [])
    }
    return resolution, merged_by_candidate


def printed_doi_status(printed_doi: str, merged: list[dict], gathered: dict) -> str:
    """Whether the DOI printed in the bibliography identifies any indexed work.

    `registered`   — at least one index returned a work carrying this DOI.
    `not_indexed`  — every DOI-capable index answered and none knows the DOI.
                     That alone does not show the DOI is invalid: many agencies'
                     DOIs are in none of the indexes. `resolve_with_providers`
                     asks doi.org, which turns this into `registered` or
                     `unregistered`.
    `unknown`      — an index that could have answered was unavailable, so the
                     absence of a match proves nothing.
    """
    if any(normalize_doi(work.get("doi")) == printed_doi for work in merged):
        return "registered"
    failed = {item.get("provider") for item in gathered.get("provider_errors", [])}
    counts = gathered.get("provider_candidate_counts", {})
    doi_capable = [name for name in ("openalex", "crossref", "datacite") if name in counts]
    if not doi_capable or failed & set(doi_capable):
        return "unknown"
    return "not_indexed"


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
    "SPECIALIST_INDEXES",
    "gather_candidates",
    "merge_key",
    "merge_records",
    "resolve_with_providers",
    "selected_abstract",
]
