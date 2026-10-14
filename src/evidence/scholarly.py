"""Scholarly index clients that speak the OpenAlex work shape.

Primary: Crossref, Semantic Scholar (OpenAlex lives in the enricher).
Supplementary, asked only for references the primary indexes cannot resolve:
dblp (computer science), Europe PMC (life sciences), DataCite (datasets,
software, preprints).

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

from src.evidence import RESOLVER_VERSION, ratelimit
from src.evidence.providers import ProviderFailure
from src.evidence.text import SPACED_HYPHEN
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




def query_title(parsed: dict) -> str:
    """The printed title as a search query. A PDF line break or justified spacing leaves
    "Li -Fi" or "2 -Gbps"; search engines may read a leading "-" as "without this word",
    so the hyphen is rejoined the way the title is printed."""
    return SPACED_HYPHEN.sub("-", str((parsed or {}).get("title") or "").strip())


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
            ratelimit.acquire(self.rate_key())
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

    def rate_key(self) -> str:
        return self.provider

    def search(self, parsed: dict) -> list[dict]:  # pragma: no cover - interface
        raise NotImplementedError


def _with_subtitle(titles, subtitles):
    """Crossref stores a book's subtitle separately; printed references join them."""
    title = (titles or [None])[0]
    subtitle = (subtitles or [None])[0]
    if title and subtitle and subtitle.casefold() not in title.casefold():
        return f"{title.rstrip(' .:')}: {subtitle}"
    return title


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
        title = query_title(parsed)
        if not title:
            return []
        params = {
            "query.bibliographic": title[:300],
            "rows": max(1, min(20, limit)),
            # `select` keeps responses small (no reference lists). If Crossref
            # ever rejects a field name, retry once without it rather than
            # losing the provider for the whole audit.
            "select": "DOI,title,subtitle,author,issued,abstract,container-title,URL,type,updated-by",
            **self._polite_params(),
        }
        try:
            document = self._get_json(f"{self._base()}/works", params)
        except ProviderFailure as error:
            if error.code != "invalid_request":
                raise
            params.pop("select", None)
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
                entry = {"display_name": display}
                orcid = author.get("ORCID") or author.get("orcid")
                if orcid:
                    entry["orcid"] = str(orcid)
                authors.append(entry)
        doi = normalize_doi(work.get("DOI"))
        subtype = str(work.get("subtype") or "").lower()
        return {
            "id": f"https://doi.org/{doi}" if doi else work.get("URL"),
            "doi": doi or None,
            "title": _with_subtitle(titles, work.get("subtitle")),
            "publication_year": int(year) if isinstance(year, int) else None,
            "authors": authors,
            "url": work.get("URL"),
            "abstract": strip_markup(work.get("abstract")),
            "primary_location": {
                "landing_page_url": work.get("URL"),
                "source": {"display_name": container[0] if container else None},
            },
            # Crossref links a retraction notice to the retracted article through
            # `updated-by`; older deposits used a record subtype instead.
            "is_retracted": (
                subtype in {"retraction", "retracted-article"}
                or any(
                    str((update or {}).get("type", "")).lower()
                    in {"retraction", "removal", "withdrawal"}
                    for update in (work.get("updated-by") or [])
                )
            ) or None,
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
        title = query_title(parsed)
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


class DblpClient(ScholarlyClient):
    """dblp computer science bibliography: conference papers Crossref often lacks.

    dblp has no abstracts; it contributes identity (title, authors, venue, year,
    DOI). It asks clients to be gentle, so its rate limit is one request per
    second and it is queried only as a supplementary index.
    """

    provider = "dblp"
    DISAMBIGUATION = re.compile(r"\s+\d{4}$")

    def _base(self) -> str:
        return str(getattr(self.config, "DBLP_BASE_URL", "https://dblp.org")).rstrip("/")

    def _mirrors(self) -> list[str]:
        """dblp.org first, then the official mirror at Trier, which answers when dblp.org is
        down or blocked by a local filter."""
        configured = getattr(self.config, "DBLP_MIRROR_URLS", ("https://dblp.uni-trier.de",))
        if isinstance(configured, str):
            configured = [item.strip() for item in configured.split(",")]
        bases = [self._base()] + [str(url).rstrip("/") for url in configured or () if url]
        return list(dict.fromkeys(bases))

    def search(self, parsed: dict) -> list[dict]:
        title = query_title(parsed)
        if not title:
            return []
        limit = int(getattr(self.config, "RESOLUTION_CANDIDATES_PER_PROVIDER", 8))
        params = {"q": title[:300], "format": "json", "h": max(1, min(20, limit))}
        mirrors = self._mirrors()
        for index, base in enumerate(mirrors):
            try:
                document = self._get_json(f"{base}/search/publ/api", params)
                break
            except ProviderFailure as error:
                # dblp has no API key: a 403 is a firewall, security suite or proxy, which the
                # mirror's other host name often gets past. So every failure tries the mirror.
                if index + 1 == len(mirrors):
                    raise
                logger.info("dblp at %s unavailable (%s); trying %s.", base, error.code, mirrors[index + 1])
        hits = ((document.get("result") or {}).get("hits") or {}).get("hit") or []
        if isinstance(hits, dict):
            hits = [hits]
        return [self.normalize(hit.get("info") or {}) for hit in hits]

    @classmethod
    def normalize(cls, info: dict) -> dict:
        raw_authors = (info.get("authors") or {}).get("author") or []
        if isinstance(raw_authors, dict):
            raw_authors = [raw_authors]
        authors = []
        for author in raw_authors:
            name = author.get("text") if isinstance(author, dict) else str(author)
            name = cls.DISAMBIGUATION.sub("", str(name or "")).strip()
            if name:
                authors.append({"display_name": name})
        doi = normalize_doi(info.get("doi"))
        if not doi:
            links = info.get("ee") or []
            for link in [links] if isinstance(links, str) else links:
                if "doi.org/" in str(link):
                    doi = normalize_doi(str(link))
                    break
        venue = info.get("venue")
        if isinstance(venue, list):
            venue = venue[0] if venue else None
        year = str(info.get("year") or "")
        title = str(info.get("title") or "").strip()
        return {
            "id": info.get("url") or (f"https://doi.org/{doi}" if doi else None),
            "doi": doi or None,
            "title": title[:-1] if title.endswith(".") else title or None,
            "publication_year": int(year) if year.isdigit() else None,
            "authors": authors,
            "url": info.get("ee") if isinstance(info.get("ee"), str) else info.get("url"),
            "abstract": None,
            "primary_location": {"landing_page_url": info.get("url"), "source": {"display_name": venue}},
            "is_retracted": None,
            "_provider": "dblp",
        }


class EuropePmcClient(ScholarlyClient):
    """Europe PMC: life-science literature with abstracts and author ORCIDs."""

    provider = "europe_pmc"

    def _base(self) -> str:
        return str(getattr(self.config, "EUROPE_PMC_BASE_URL",
                           "https://www.ebi.ac.uk/europepmc/webservices/rest")).rstrip("/")

    @staticmethod
    def _quoted(text: str) -> str:
        return '"' + re.sub(r'["\\]', " ", text).strip() + '"'

    def search(self, parsed: dict) -> list[dict]:
        doi = normalize_doi(parsed.get("doi")).strip()
        title = query_title(parsed)
        if doi:
            query = f"DOI:{self._quoted(doi)}"
        elif title:
            query = f"TITLE:{self._quoted(title[:300])}"
        else:
            return []
        limit = int(getattr(self.config, "RESOLUTION_CANDIDATES_PER_PROVIDER", 8))
        document = self._get_json(f"{self._base()}/search",
                                  {"query": query, "format": "json", "resultType": "core",
                                   "pageSize": max(1, min(20, limit))})
        results = (document.get("resultList") or {}).get("result") or []
        if not results and doi and title:
            document = self._get_json(f"{self._base()}/search",
                                      {"query": f"TITLE:{self._quoted(title[:300])}", "format": "json",
                                       "resultType": "core", "pageSize": max(1, min(20, limit))})
            results = (document.get("resultList") or {}).get("result") or []
        return [self.normalize(item) for item in results]

    @staticmethod
    def normalize(work: dict) -> dict:
        authors = []
        for author in ((work.get("authorList") or {}).get("author") or []):
            last, first = (author.get("lastName") or "").strip(), (author.get("firstName") or "").strip()
            display = f"{last}, {first}".strip(", ") or author.get("fullName") or ""
            if not display:
                continue
            entry = {"display_name": display}
            identifier = author.get("authorId") or {}
            if str(identifier.get("type", "")).upper() == "ORCID" and identifier.get("value"):
                entry["orcid"] = str(identifier["value"])
            authors.append(entry)
        if not authors and work.get("authorString"):
            authors = [{"display_name": name.strip().rstrip(".")}
                       for name in str(work["authorString"]).split(",") if name.strip()]
        journal = ((work.get("journalInfo") or {}).get("journal") or {}).get("title")
        doi = normalize_doi(work.get("doi"))
        source, identifier = work.get("source"), work.get("id")
        year = str(work.get("pubYear") or "")
        types = {str(item).lower() for item in ((work.get("pubTypeList") or {}).get("pubType") or [])}
        external = {}
        if work.get("pmid"):
            external["PubMed"] = str(work["pmid"])
        if work.get("pmcid"):
            external["PubMedCentral"] = str(work["pmcid"])
        url = f"https://europepmc.org/article/{source}/{identifier}" if source and identifier else None
        return {
            "id": url or (f"https://doi.org/{doi}" if doi else None),
            "doi": doi or None,
            "title": str(work.get("title") or "").strip().rstrip(".") or None,
            "publication_year": int(year) if year.isdigit() else None,
            "authors": authors,
            "url": url,
            "abstract": strip_markup(work.get("abstractText")),
            "primary_location": {"landing_page_url": url, "source": {"display_name": journal}},
            "is_retracted": True if "retracted publication" in types else None,
            "external_ids": external,
            "_provider": "europe_pmc",
        }


class DoiRegistryClient(ScholarlyClient):
    """doi.org: the resolver every DOI registration agency answers through.

    Crossref, DataCite, mEDRA, JaLC, KISTI, CNKI, Airiti and others all register
    their DOIs in the Handle System behind doi.org. Absence from two or three
    scholarly indexes does not show that a DOI was never registered; only this
    check does.
    """

    provider = "doi_org"

    def _base(self) -> str:
        return str(getattr(self.config, "DOI_RESOLVER_URL", "https://doi.org")).rstrip("/")

    def registered(self, doi: str) -> bool | None:
        """True or False when doi.org answered; None when it could not be asked."""
        value = normalize_doi(doi).strip()
        if not re.fullmatch(r"10\.\d{4,9}/\S+", value):
            return False
        try:
            document = self._get_json(f"{self._base()}/api/handles/{quote(value, safe='/')}", {})
        except ProviderFailure as error:
            if error.code == "model_unavailable":      # HTTP 404: no such handle
                return False
            return None
        except Exception:  # noqa: BLE001 - a malformed reply leaves registration unknown
            return None
        code = document.get("responseCode") if isinstance(document, dict) else None
        return True if code == 1 else False if code in (100, 200) else None

    def search(self, parsed: dict) -> list[dict]:  # registration only; never a candidate source
        return []


class DataCiteClient(ScholarlyClient):
    """DataCite: DOIs for datasets, software, preprints (arXiv) and Zenodo records.

    Crossref does not hold DataCite DOIs, so without this index a correct
    dataset or software DOI could be reported as unregistered.
    """

    provider = "datacite"

    def _base(self) -> str:
        return str(getattr(self.config, "DATACITE_BASE_URL", "https://api.datacite.org")).rstrip("/")

    def search(self, parsed: dict) -> list[dict]:
        doi = normalize_doi(parsed.get("doi")).strip()
        if doi:
            try:
                document = self._get_json(f"{self._base()}/dois/{quote(doi, safe='')}", {})
                if document.get("data"):
                    return [self.normalize(document["data"])]
            except ProviderFailure as error:
                if error.code != "model_unavailable":  # 404: not a DataCite DOI
                    raise
        title = query_title(parsed)
        if not title:
            return []
        limit = int(getattr(self.config, "RESOLUTION_CANDIDATES_PER_PROVIDER", 8))
        document = self._get_json(f"{self._base()}/dois",
                                  {"query": title[:300], "page[size]": max(1, min(20, limit))})
        return [self.normalize(item) for item in document.get("data") or []]

    @staticmethod
    def normalize(item: dict) -> dict:
        attributes = item.get("attributes") or {}
        authors = []
        for creator in attributes.get("creators") or []:
            family, given = (creator.get("familyName") or "").strip(), (creator.get("givenName") or "").strip()
            display = f"{family}, {given}".strip(", ") or creator.get("name") or ""
            if not display:
                continue
            entry = {"display_name": display}
            for identifier in creator.get("nameIdentifiers") or []:
                if str(identifier.get("nameIdentifierScheme", "")).upper() == "ORCID" and identifier.get("nameIdentifier"):
                    entry["orcid"] = str(identifier["nameIdentifier"])
            authors.append(entry)
        titles = attributes.get("titles") or []
        abstract = next((d.get("description") for d in attributes.get("descriptions") or []
                         if str(d.get("descriptionType", "")).lower() == "abstract"), None)
        publisher = attributes.get("publisher")
        if isinstance(publisher, dict):
            publisher = publisher.get("name")
        container = (attributes.get("container") or {}).get("title")
        doi = normalize_doi(attributes.get("doi") or item.get("id"))
        year = attributes.get("publicationYear")
        return {
            "id": f"https://doi.org/{doi}" if doi else attributes.get("url"),
            "doi": doi or None,
            "title": (titles[0].get("title") if titles else None),
            "publication_year": int(year) if str(year or "").isdigit() else None,
            "authors": authors,
            "url": attributes.get("url"),
            "abstract": strip_markup(abstract),
            "primary_location": {"landing_page_url": attributes.get("url"),
                                 "source": {"display_name": container or publisher}},
            "is_retracted": None,
            "resource_type": (attributes.get("types") or {}).get("resourceTypeGeneral"),
            "_provider": "datacite",
        }


SUPPLEMENTARY = {"dblp": DblpClient, "europe_pmc": EuropePmcClient, "datacite": DataCiteClient}


def build_supplementary_clients(config, recorder, session=None) -> dict:
    """Specialist indexes asked only when the primary providers cannot resolve a reference."""
    if getattr(config, "OFFLINE_MODE", False):
        return {}
    enabled = tuple(getattr(config, "SUPPLEMENTARY_PROVIDERS", ()))
    clients = {name: SUPPLEMENTARY[name](config, recorder, session) for name in enabled if name in SUPPLEMENTARY}
    if semantic_scholar_deferred(config):
        clients["semantic_scholar"] = SemanticScholarClient(config, recorder, session)
    return clients


def semantic_scholar_deferred(config) -> bool:
    """Without an API key, Semantic Scholar shares one small anonymous quota.

    Asking it for every reference produced "too many requests" errors that left
    references unchecked even when OpenAlex and Crossref had already found them.
    Keyless, it is asked only for references the other indexes cannot resolve.
    """
    enabled = tuple(getattr(config, "RESOLUTION_PROVIDERS", ("openalex",)))
    return "semantic_scholar" in enabled and not getattr(config, "SEMANTIC_SCHOLAR_API_KEY", None)


def build_clients(config, recorder, session=None) -> dict:
    """Instantiate only the providers this deployment enabled."""
    enabled = tuple(getattr(config, "RESOLUTION_PROVIDERS", ("openalex",)))
    clients = {}
    if "crossref" in enabled:
        clients["crossref"] = CrossrefClient(config, recorder, session)
    if "semantic_scholar" in enabled and not semantic_scholar_deferred(config):
        clients["semantic_scholar"] = SemanticScholarClient(config, recorder, session)
    return clients


__all__ = [
    "DataCiteClient",
    "DoiRegistryClient",
    "DblpClient",
    "EuropePmcClient",
    "build_supplementary_clients",
    "CrossrefClient",
    "ScholarlyClient",
    "SemanticScholarClient",
    "build_clients",
    "strip_markup",
]
