"""Crossref and Semantic Scholar clients that speak the OpenAlex work shape.

`src.evidence.identity.resolve_candidates` already ranks candidates using
title similarity, author surname overlap, DOI equality and year. Rather than
writing a second ranker, each provider here normalizes its response into the
same dictionary shape that function already understands, so all three providers
compete on one scale and one conflict policy.

Every call goes through the Phase 2 `CallRecorder`, which writes an exact
request/response artifact and never records credentials.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote

from src.evidence import RESOLVER_VERSION
from src.evidence.providers import ProviderFailure
from src.utils.reference_matching import normalize_doi

logger = logging.getLogger(__name__)

TAG = re.compile(r"<[^>]+>")


def strip_markup(value: str | None) -> str | None:
    """Crossref abstracts are JATS XML; keep the text and drop the tags."""
    if not value:
        return None
    try:
        from bs4 import BeautifulSoup

        text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    except Exception:  # noqa: BLE001 - fall back to a conservative regex
        text = TAG.sub(" ", value)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


class ScholarlyClient:
    """Base class holding the shared session and recorder plumbing."""

    provider = "unknown"

    def __init__(self, config, recorder, session=None):
        self.config = config
        self.recorder = recorder
        if session is None:
            import requests

            session = requests.Session()
            session.headers["User-Agent"] = "CitePrism/4.0 evidence-review"
        self.session = session
        self.timeout = float(getattr(config, "RESOLUTION_TIMEOUT_SECONDS", 35.0))

    def _get_json(self, url: str, params: dict, headers: dict | None = None) -> dict:
        transport = dict(params)
        request_headers = dict(headers or {})

        def invoke():
            response = self.session.get(
                url, params=transport, headers=request_headers,
                timeout=(10, self.timeout),
            )
            response.raise_for_status()
            return response.json()

        raw = self.recorder.call(
            provider=self.provider,
            model="metadata-api",
            purpose="reference_resolution",
            prompt_version=RESOLVER_VERSION,
            # Only the public query is recorded; API keys stay in headers.
            request={"url": url, "params": params},
            invoke=invoke,
            decode=lambda value: (json.dumps(value, ensure_ascii=False), None),
        )
        return json.loads(raw)

    def search(self, parsed: dict) -> list[dict]:  # pragma: no cover - interface
        raise NotImplementedError


class CrossrefClient(ScholarlyClient):
    """Crossref REST API. DOI lookup first, then a bibliographic query."""

    provider = "crossref"

    def _base(self) -> str:
        return str(getattr(self.config, "CROSSREF_BASE_URL", "https://api.crossref.org")).rstrip("/")

    def _polite_params(self) -> dict:
        mailto = getattr(self.config, "CROSSREF_MAILTO", None) or getattr(
            self.config, "OPENALEX_EMAIL", None
        )
        # Crossref's polite pool gives better availability to identified callers.
        return {"mailto": mailto} if mailto and "@" in str(mailto) else {}

    def search(self, parsed: dict) -> list[dict]:
        doi = normalize_doi(parsed.get("doi")).strip()
        limit = int(getattr(self.config, "RESOLUTION_CANDIDATES_PER_PROVIDER", 8))
        if doi:
            try:
                document = self._get_json(
                    f"{self._base()}/works/{quote(doi, safe='')}", self._polite_params()
                )
                message = document.get("message")
                if message:
                    return [self.normalize(message)]
            except ProviderFailure as error:
                if error.code != "model_unavailable":  # 404 simply means no such DOI
                    raise
        title = (parsed.get("title") or "").strip()
        if not title:
            return []
        params = {
            "query.bibliographic": title[:300],
            "rows": max(1, min(20, limit)),
            "select": "DOI,title,author,issued,abstract,container-title,URL,type,subtype",
            **self._polite_params(),
        }
        document = self._get_json(f"{self._base()}/works", params)
        items = (document.get("message") or {}).get("items", [])
        return [self.normalize(item) for item in items]

    @staticmethod
    def normalize(work: dict) -> dict:
        titles = work.get("title") or []
        container = work.get("container-title") or []
        issued = ((work.get("issued") or {}).get("date-parts") or [[None]])[0]
        year = issued[0] if issued else None
        authors = []
        for author in work.get("author") or []:
            family = (author.get("family") or "").strip()
            given = (author.get("given") or "").strip()
            display = f"{family}, {given}".strip(", ") or author.get("name") or ""
            if display:
                authors.append({"display_name": display})
        doi = normalize_doi(work.get("DOI"))
        subtype = str(work.get("subtype") or "").lower()
        return {
            "id": f"https://doi.org/{doi}" if doi else work.get("URL"),
            "doi": doi or None,
            "title": titles[0] if titles else None,
            "publication_year": int(year) if isinstance(year, int) else None,
            "authors": authors,
            "url": work.get("URL"),
            "abstract": strip_markup(work.get("abstract")),
            "primary_location": {
                "landing_page_url": work.get("URL"),
                "source": {"display_name": container[0] if container else None},
            },
            # Crossref publishes retractions as a separate record type.
            "is_retracted": subtype in {"retraction", "retracted-article"} or None,
            "_provider": "crossref",
        }


class SemanticScholarClient(ScholarlyClient):
    """Semantic Scholar Graph API. Adds coverage for preprints and CS venues."""

    provider = "semantic_scholar"
    FIELDS = (
        "paperId,externalIds,title,abstract,year,venue,url,"
        "authors.name,publicationTypes,isOpenAccess,openAccessPdf"
    )

    def _headers(self) -> dict:
        key = getattr(self.config, "SEMANTIC_SCHOLAR_API_KEY", None)
        return {"x-api-key": key} if key else {}

    def _base(self) -> str:
        return str(
            getattr(self.config, "SEMANTIC_SCHOLAR_BASE_URL",
                    "https://api.semanticscholar.org/graph/v1")
        ).rstrip("/")

    def search(self, parsed: dict) -> list[dict]:
        doi = normalize_doi(parsed.get("doi")).strip()
        limit = int(getattr(self.config, "RESOLUTION_CANDIDATES_PER_PROVIDER", 8))
        if doi:
            try:
                document = self._get_json(
                    f"{self._base()}/paper/DOI:{quote(doi, safe='')}",
                    {"fields": self.FIELDS},
                    self._headers(),
                )
                if document:
                    return [self.normalize(document)]
            except ProviderFailure as error:
                if error.code != "model_unavailable":
                    raise
        title = (parsed.get("title") or "").strip()
        if not title:
            return []
        document = self._get_json(
            f"{self._base()}/paper/search",
            {"query": title[:300], "limit": max(1, min(20, limit)), "fields": self.FIELDS},
            self._headers(),
        )
        return [self.normalize(item) for item in document.get("data", [])]

    @staticmethod
    def normalize(work: dict) -> dict:
        external = work.get("externalIds") or {}
        doi = normalize_doi(external.get("DOI"))
        identifier = work.get("paperId")
        publication_types = [
            str(item).lower() for item in (work.get("publicationTypes") or [])
        ]
        open_access = work.get("openAccessPdf") or {}
        return {
            "id": (
                f"https://www.semanticscholar.org/paper/{identifier}"
                if identifier
                else (f"https://doi.org/{doi}" if doi else None)
            ),
            "doi": doi or None,
            "title": work.get("title"),
            "publication_year": work.get("year"),
            "authors": [
                {"display_name": author.get("name")}
                for author in (work.get("authors") or [])
                if author.get("name")
            ],
            "url": work.get("url"),
            "abstract": work.get("abstract"),
            "primary_location": {
                "landing_page_url": work.get("url"),
                "source": {"display_name": work.get("venue")},
            },
            "is_retracted": True if "retraction" in publication_types else None,
            "open_access_pdf_url": open_access.get("url"),
            "external_ids": {
                key: value
                for key, value in external.items()
                if key in {"DOI", "ArXiv", "PubMed", "PubMedCentral", "CorpusId"}
            },
            "_provider": "semantic_scholar",
        }


def build_clients(config, recorder, session=None) -> dict:
    """Instantiate only the providers this deployment enabled."""
    enabled = tuple(getattr(config, "RESOLUTION_PROVIDERS", ("openalex",)))
    clients = {}
    if "crossref" in enabled:
        clients["crossref"] = CrossrefClient(config, recorder, session)
    if "semantic_scholar" in enabled:
        clients["semantic_scholar"] = SemanticScholarClient(config, recorder, session)
    return clients


__all__ = [
    "CrossrefClient",
    "ScholarlyClient",
    "SemanticScholarClient",
    "build_clients",
    "strip_markup",
]
