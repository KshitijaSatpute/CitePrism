"""Phase 4 enrichment: multi-provider identity plus optional full-text evidence.

This subclasses `OpenAlexEnricher`, which owns the loop (cache, concurrency,
ordering, extraction-status guard). Only the per-reference lookup changes:
OpenAlex, Crossref and Semantic Scholar are queried, merged on DOI, ranked by
the existing rule-based resolver, and the *merged* record behind the selected
candidate supplies the abstract, citation-graph fields and open-access links.

When only OpenAlex is enabled and full text is off, `build_enricher` returns the
plain `OpenAlexEnricher`, so behaviour is identical to Phase 2.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from src.enrichers.openalex_enricher import OpenAlexEnricher, graph_fields, reconstruct_abstract
from src.evidence import RESOLVER_VERSION
from src.evidence.fulltext import FullTextAcquirer
from src.evidence.providers import ProviderFailure, utc_now
from src.evidence.resolution import resolve_with_providers
from src.evidence.scholarly import DoiRegistryClient, build_clients, build_supplementary_clients, strip_markup
from src.evidence.text import digest_json, digest_text
from src.utils.reference_matching import normalize_doi

logger = logging.getLogger(__name__)

ENRICHMENT_VERSION = f"{RESOLVER_VERSION}+multi-provider.5"


def uses_phase4(config) -> bool:
    providers = tuple(getattr(config, "RESOLUTION_PROVIDERS", ("openalex",)))
    return bool(set(providers) - {"openalex"}) or bool(
        getattr(config, "ENABLE_FULL_TEXT_ACQUISITION", False)
    )


def expected_enrichment_version(config) -> str:
    """The `method_version` the selected enricher writes.

    The orchestrator compares a stored artifact against this to decide whether
    it can be reused. Comparing against the wrong constant silently forces a
    full re-enrichment and re-scoring of every audit.
    """
    return ENRICHMENT_VERSION if uses_phase4(config) else RESOLVER_VERSION


class Phase4Enricher(OpenAlexEnricher):
    """Identity resolution across OpenAlex, Crossref and Semantic Scholar."""

    def __init__(self, config, db_manager):
        super().__init__(config, db_manager)
        self.enabled_providers = tuple(getattr(config, "RESOLUTION_PROVIDERS", ("openalex",)))
        self.clients = build_clients(config, self.recorder, self.session)
        self.supplementary = build_supplementary_clients(config, self.recorder, self.session)
        # doi.org decides whether a printed DOI exists; without it a DOI is never called invalid.
        self.registry = (None if getattr(config, "OFFLINE_MODE", False)
                         or not getattr(config, "DOI_REGISTRY_CHECK", True)
                         else DoiRegistryClient(config, self.recorder, self.session))
        self.full_text = FullTextAcquirer(config, self.recorder, self.session)

    def method_version(self):
        return ENRICHMENT_VERSION

    def cache_namespace(self):
        return {
            "resolver": ENRICHMENT_VERSION,
            "providers": sorted(self.enabled_providers),
            "supplementary": sorted(self.supplementary),
            "full_text": bool(getattr(self.config, "ENABLE_FULL_TEXT_ACQUISITION", False)),
            "unpaywall": bool(getattr(self.config, "ENABLE_UNPAYWALL", False)),
            "graph": 1,
            "doi_registry": self.registry is not None,
        }

    # ------------------------------------------------------------------
    def _openalex_candidates(self, parsed: dict) -> list[dict] | None:
        """OpenAlex works normalized with their reconstructed abstracts; None when OpenAlex is
        not enabled, so it is not reported as searched."""
        if "openalex" not in self.enabled_providers:
            return None
        normalized = []
        for work in self._openalex_works(parsed):
            record = dict(work)
            record["_provider"] = "openalex"
            record["abstract"] = reconstruct_abstract(work.get("abstract_inverted_index"))
            record.pop("abstract_inverted_index", None)
            location = work.get("primary_location") or {}
            best = work.get("best_oa_location") or {}
            if best.get("pdf_url"):
                record["open_access_pdf_url"] = best["pdf_url"]
            record["url"] = location.get("landing_page_url") or work.get("url")
            normalized.append(record)
        return normalized

    def _lookup(self, parsed: dict) -> dict:
        call_start = len(self.recorder.records)

        openalex_error = None
        try:
            openalex_candidates = self._openalex_candidates(parsed)
        except ProviderFailure as error:
            openalex_candidates, openalex_error = [], error

        resolution, merged = resolve_with_providers(
            parsed, self.clients, openalex_candidates=openalex_candidates, return_merged=True,
            supplementary=self.supplementary, registry=self.registry,
        )
        if openalex_error is not None:
            resolution.setdefault("provider_errors", []).append(
                {"provider": "openalex", "error_code": openalex_error.code,
                 **({"http_status": openalex_error.status_code} if openalex_error.status_code else {})}
            )
            if resolution["status"] in {"not_found", "unresolved"} and not resolution.get("printed_doi_record"):
                resolution["status"] = "provider_unavailable"
                resolution["error_code"] = openalex_error.code
                resolution.pop("search_incomplete", None)
            if resolution.get("printed_doi_status") == "not_indexed":
                resolution["printed_doi_status"] = "unknown"

        selected = next((item for item in resolution["candidates"] if item.get("selected")), None)
        external: dict = dict(selected["metadata"]) if selected else {}
        snapshot = None

        if selected:
            work = merged.get(selected["candidate_id"]) or {}
            external["providers"] = selected.get("providers", [])
            external["provider_agreement"] = selected.get("provider_agreement", 1)
            if selected.get("external_ids") or work.get("external_ids"):
                external["external_ids"] = selected.get("external_ids") or work.get("external_ids")
            external["graph"] = graph_fields(work)
            if work.get("is_retracted") is True:
                external["is_retracted"] = True

            snapshot = self._acquire_source_text(selected, work, external)
            external.update(
                {
                    "abstract": (snapshot or {}).get("text") if (snapshot or {}).get("kind") == "abstract"
                    else work.get("abstract"),
                    "abstract_source": (snapshot or {}).get("provider"),
                    "evidence_kind": (snapshot or {}).get("kind"),
                    "match_method": resolution["method"],
                    "match_confidence": None,
                    "is_scraped": False,
                }
            )

        return {
            "resolution": resolution,
            "external_metadata": external,
            "source_snapshot": snapshot,
            "lookup_observed_at": utc_now(),
            "lookup_calls": self.recorder.records[call_start:],
        }

    def _acquire_source_text(self, candidate: dict, work: dict, external: dict) -> dict | None:
        """Prefer open-access full text; fall back to the merged abstract."""
        if getattr(self.config, "ENABLE_FULL_TEXT_ACQUISITION", False):
            locator = {
                **work,
                "candidate_id": candidate["candidate_id"],
                "external_ids": candidate.get("external_ids") or work.get("external_ids") or {},
                "open_access_pdf_url": candidate.get("open_access_pdf_url")
                or work.get("open_access_pdf_url"),
            }
            oa_url = (external.get("graph") or {}).get("open_access_url")
            try:
                full = self.full_text.acquire(locator, {**external, "open_access_url": oa_url})
            except Exception:  # noqa: BLE001 - never fail an audit over full text
                logger.exception("Full-text acquisition raised; using the abstract instead.")
                full = None
            if full:
                # Keep the abstract alongside — the assessment prompt shows it
                # first — and bring it under the snapshot's integrity digest.
                abstract = work.get("abstract")
                if abstract:
                    full["abstract"] = abstract
                    full["abstract_sha256"] = digest_text(abstract)
                    full.pop("snapshot_id", None)
                    full["snapshot_id"] = digest_json(full)
                return full

        abstract = work.get("abstract")
        provider = work.get("abstract_provider") or work.get("_provider") or "openalex"
        if not abstract:
            abstract, provider = self._crossref_abstract(external)
        if not abstract:
            return None
        snapshot = {
            "text": abstract,
            "text_sha256": digest_text(abstract),
            "kind": "abstract",
            "provider": provider,
            "url": work.get("url"),
            "retrieved_at": utc_now(),
            "candidate_id": candidate["candidate_id"],
            "doi": external.get("doi"),
        }
        snapshot["snapshot_id"] = digest_json(snapshot)
        return snapshot

    def _crossref_abstract(self, external: dict) -> tuple[str | None, str]:
        """DOI-keyed Crossref abstract fallback, as in Phase 2."""
        doi = normalize_doi(external.get("doi")).strip()
        if not doi:
            return None, "unknown"
        url = f"https://api.crossref.org/works/{quote(doi, safe='')}"
        try:
            response = self._get_json("crossref", url, {})
        except ProviderFailure:
            return None, "unknown"
        message = response.get("message", {})
        if normalize_doi(message.get("DOI")) != doi:
            return None, "unknown"
        return strip_markup(message.get("abstract")), "crossref"

    def enrich_references(self, parsed_data, force=False):
        result = super().enrich_references(parsed_data, force=force)
        metadata = result.setdefault("enrichment_metadata", {})
        metadata["providers"] = list(self.enabled_providers)
        metadata["supplementary_providers"] = sorted(self.supplementary)
        metadata["full_text_enabled"] = bool(
            getattr(self.config, "ENABLE_FULL_TEXT_ACQUISITION", False)
        )
        references = result.get("enriched_references", [])
        summary = result.setdefault("enrichment_summary", {})
        kinds = [(item.get("source_snapshot") or {}).get("kind") for item in references]
        summary["full_text_snapshots"] = kinds.count("full_text")
        summary["abstract_snapshots"] = kinds.count("abstract")
        summary["multi_provider_agreement"] = sum(
            1
            for item in references
            if int((item.get("external_metadata") or {}).get("provider_agreement") or 0) > 1
        )
        return result


def build_enricher(config, db_manager):
    """Return the Phase 4 enricher when extra providers or full text are on."""
    if uses_phase4(config):
        return Phase4Enricher(config, db_manager)
    return OpenAlexEnricher(config, db_manager)


__all__ = [
    "ENRICHMENT_VERSION",
    "Phase4Enricher",
    "build_enricher",
    "expected_enrichment_version",
    "uses_phase4",
]
