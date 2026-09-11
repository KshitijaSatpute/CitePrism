"""Phase 4 enrichment: multi-provider identity plus optional full-text evidence.

This subclasses the verified Phase 2 `OpenAlexEnricher` rather than replacing
it. Caching, staleness, artifact recording, the extraction-status guard and the
cache key all keep working exactly as they did; only the lookup step changes.

When the Phase 4 features are switched off, the behaviour is identical to
Phase 2 — which is what makes the upgrade safe to deploy incrementally.
"""

from __future__ import annotations

import copy
import logging
from urllib.parse import quote

from src.enrichers.openalex_enricher import OpenAlexEnricher, reconstruct_abstract
from src.evidence import RESOLVER_VERSION
from src.evidence.fulltext import FullTextAcquirer
from src.evidence.providers import ProviderFailure, utc_now
from src.evidence.resolution import resolve_with_providers
from src.evidence.scholarly import build_clients
from src.evidence.text import digest_json, digest_text
from src.utils.reference_matching import normalize_doi

logger = logging.getLogger(__name__)

ENRICHMENT_VERSION = f"{RESOLVER_VERSION}+multi-provider"


class Phase4Enricher(OpenAlexEnricher):
    """Identity resolution across OpenAlex, Crossref and Semantic Scholar."""

    def __init__(self, config, db_manager):
        super().__init__(config, db_manager)
        self.clients = build_clients(config, self.recorder, self.session)
        self.full_text = FullTextAcquirer(config, self.recorder, self.session)
        self.enabled_providers = tuple(
            getattr(config, "RESOLUTION_PROVIDERS", ("openalex",))
        )

    def _openalex_candidates(self, parsed: dict) -> list[dict]:
        """The Phase 2 OpenAlex query, unchanged, returning raw works."""
        if "openalex" not in self.enabled_providers:
            return []
        doi = normalize_doi(parsed.get("doi")).strip()
        candidates: list[dict] = []
        try:
            if doi:
                candidates = self._get_json(
                    "openalex",
                    "https://api.openalex.org/works",
                    {"filter": f"doi:https://doi.org/{doi}", "per-page": 5},
                ).get("results", [])
            if not candidates and parsed.get("title"):
                candidates = self._get_json(
                    "openalex",
                    "https://api.openalex.org/works",
                    {"search": parsed["title"], "per-page": 10},
                ).get("results", [])
        except ProviderFailure as error:
            logger.info("OpenAlex unavailable during resolution: %s", error.code)
            raise
        normalized = []
        for work in candidates:
            record = dict(work)
            record["_provider"] = "openalex"
            record["abstract"] = reconstruct_abstract(work.get("abstract_inverted_index"))
            location = work.get("primary_location") or {}
            best = work.get("best_oa_location") or {}
            if best.get("pdf_url"):
                record["open_access_pdf_url"] = best["pdf_url"]
            record["url"] = location.get("landing_page_url") or work.get("url")
            normalized.append(record)
        return normalized

    def _lookup(self, parsed: dict) -> dict:
        """Resolve identity across every enabled index, then acquire source text."""
        call_start = len(self.recorder.records)

        openalex_error = None
        try:
            openalex_candidates = self._openalex_candidates(parsed)
        except ProviderFailure as error:
            openalex_candidates, openalex_error = [], error

        resolution = resolve_with_providers(
            parsed, self.clients, openalex_candidates=openalex_candidates
        )
        if openalex_error is not None:
            resolution.setdefault("provider_errors", []).append(
                {"provider": "openalex", "error_code": openalex_error.code}
            )
            if resolution["status"] in {"not_found", "unresolved"}:
                resolution["status"] = "provider_unavailable"
                resolution["error_code"] = openalex_error.code

        selected = next(
            (item for item in resolution["candidates"] if item.get("selected")), None
        )
        external: dict = dict(selected["metadata"]) if selected else {}
        snapshot = None

        if selected:
            external["providers"] = selected.get("providers", [])
            external["provider_agreement"] = selected.get("provider_agreement", 1)
            if selected.get("external_ids"):
                external["external_ids"] = selected["external_ids"]

            snapshot = self._acquire_source_text(selected, external, resolution)
            external.update(
                {
                    "abstract": snapshot.get("text") if snapshot else None,
                    "abstract_source": snapshot.get("provider") if snapshot else None,
                    "evidence_kind": snapshot.get("kind") if snapshot else None,
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

    def _acquire_source_text(self, candidate: dict, external: dict, resolution: dict) -> dict | None:
        """Prefer open-access full text; fall back to the abstract."""
        merged = self._merged_candidate(candidate, resolution)

        if getattr(self.config, "ENABLE_FULL_TEXT_ACQUISITION", False):
            try:
                full = self.full_text.acquire(
                    {**merged, "candidate_id": candidate["candidate_id"]}, external
                )
            except Exception:  # noqa: BLE001 - never fail an audit over full text
                logger.exception("Full-text acquisition raised; using the abstract instead.")
                full = None
            if full:
                return full

        abstract = merged.get("abstract")
        provider = merged.get("abstract_provider") or merged.get("_provider") or "openalex"
        if not abstract:
            abstract, provider = self._crossref_abstract(external)
        if not abstract:
            return None
        snapshot = {
            "text": abstract,
            "text_sha256": digest_text(abstract),
            "kind": "abstract",
            "provider": provider,
            "url": merged.get("url"),
            "retrieved_at": utc_now(),
            "candidate_id": candidate["candidate_id"],
            "doi": external.get("doi"),
        }
        snapshot["snapshot_id"] = digest_json(snapshot)
        return snapshot

    @staticmethod
    def _merged_candidate(candidate: dict, resolution: dict) -> dict:
        """Recover the merged provider record behind a ranked candidate."""
        merged = dict(candidate.get("metadata") or {})
        for key in ("open_access_pdf_url", "external_ids", "providers"):
            if candidate.get(key):
                merged[key] = candidate[key]
        merged.setdefault("abstract", candidate.get("abstract"))
        merged.setdefault("_provider", (candidate.get("providers") or ["openalex"])[0])
        del resolution
        return merged

    def _crossref_abstract(self, external: dict) -> tuple[str | None, str]:
        """DOI-keyed Crossref abstract fallback, as in Phase 2."""
        doi = normalize_doi(external.get("doi")).strip()
        if not doi or "crossref" not in self.clients:
            return None, "unknown"
        url = f"https://api.crossref.org/works/{quote(doi, safe='')}"
        try:
            response = self._get_json("crossref", url, {})
        except ProviderFailure:
            return None, "unknown"
        message = response.get("message", {})
        if normalize_doi(message.get("DOI")) != doi:
            return None, "unknown"
        from src.evidence.scholarly import strip_markup

        return strip_markup(message.get("abstract")), "crossref"

    def enrich_references(self, parsed_data, force=False):
        result = super().enrich_references(parsed_data, force=force)
        metadata = result.setdefault("enrichment_metadata", {})
        metadata["method_version"] = ENRICHMENT_VERSION
        metadata["providers"] = list(self.enabled_providers)
        metadata["full_text_enabled"] = bool(
            getattr(self.config, "ENABLE_FULL_TEXT_ACQUISITION", False)
        )
        references = result.get("enriched_references", [])
        summary = result.setdefault("enrichment_summary", {})
        summary["full_text_snapshots"] = sum(
            1
            for item in references
            if (item.get("source_snapshot") or {}).get("kind") == "full_text"
        )
        summary["abstract_snapshots"] = sum(
            1
            for item in references
            if (item.get("source_snapshot") or {}).get("kind") == "abstract"
        )
        summary["multi_provider_agreement"] = sum(
            1
            for item in references
            if int((item.get("external_metadata") or {}).get("provider_agreement", 0)) > 1
        )
        return result


def build_enricher(config, db_manager):
    """Return the Phase 4 enricher when extra providers or full text are on."""
    providers = tuple(getattr(config, "RESOLUTION_PROVIDERS", ("openalex",)))
    extras = set(providers) - {"openalex"}
    if extras or getattr(config, "ENABLE_FULL_TEXT_ACQUISITION", False):
        return Phase4Enricher(config, db_manager)
    return OpenAlexEnricher(config, db_manager)


__all__ = ["ENRICHMENT_VERSION", "Phase4Enricher", "build_enricher"]
