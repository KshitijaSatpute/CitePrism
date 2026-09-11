"""Open-access full-text acquisition for source claim checking.

Phase 3 assessed abstracts only, and said so in every export: an abstract cannot
establish that a full paper fails to support a claim. Phase 4 acquires the full
text where it is lawfully available, so "insufficient evidence" starts to mean
something about the paper rather than about our retrieval.

Scope is deliberately narrow. Only open-access locations are fetched — a
publisher-hosted PDF behind a licence is never scraped, and `robots`-hostile
retrieval is not attempted. When no open location exists, the snapshot stays an
abstract and is labeled as one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import quote, urlparse

from src.evidence.providers import ProviderFailure, utc_now
from src.evidence.text import digest_json, digest_text
from src.utils.reference_matching import normalize_doi

logger = logging.getLogger(__name__)

FULL_TEXT_VERSION = "fulltext-v1"
PDF_MAGIC = b"%PDF-"

# Sections after which a reference list starts; full text is truncated there so
# a "quotation" is never located inside the cited paper's own bibliography.
TAIL_SECTIONS = re.compile(
    r"(?im)^\s*(references|bibliography|works\s+cited|literature\s+cited)\s*$"
)


@dataclass
class FullTextResult:
    text: str
    kind: str  # full_text | abstract
    provider: str
    url: str
    license: str | None = None
    truncated: bool = False

    def snapshot(self, *, candidate_id: str | None, doi: str | None) -> dict:
        snapshot = {
            "text": self.text,
            "text_sha256": digest_text(self.text),
            "kind": self.kind,
            "provider": self.provider,
            "url": self.url,
            "license": self.license,
            "truncated": self.truncated,
            "retrieved_at": utc_now(),
            "candidate_id": candidate_id,
            "doi": doi,
            "method_version": FULL_TEXT_VERSION,
        }
        snapshot["snapshot_id"] = digest_json(snapshot)
        return snapshot


def _https_only(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(str(url))
    return str(url) if parsed.scheme == "https" and parsed.netloc else None


def extract_pdf_text(payload: bytes, *, max_chars: int) -> str:
    import io

    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(payload))
    if reader.is_encrypted:
        raise ValueError("the retrieved PDF is encrypted")
    parts, total = [], 0
    for page in reader.pages:
        text = page.extract_text() or ""
        parts.append(text)
        total += len(text)
        if total >= max_chars:
            break
    return "\n\n".join(parts)


def strip_reference_section(text: str) -> tuple[str, bool]:
    """Cut a trailing reference list so quotes cannot be matched inside it."""
    matches = list(TAIL_SECTIONS.finditer(text))
    if not matches:
        return text, False
    boundary = matches[-1]
    # Only trust a boundary in the last third of the document.
    if boundary.start() < len(text) * 0.5:
        return text, False
    return text[: boundary.start()].rstrip(), True


class FullTextAcquirer:
    """Locate and retrieve open-access full text for a resolved work."""

    def __init__(self, config, recorder, session=None):
        self.config = config
        self.recorder = recorder
        if session is None:
            import requests

            session = requests.Session()
            session.headers["User-Agent"] = "CitePrism/4.0 evidence-review"
        self.session = session
        self.max_bytes = int(getattr(config, "FULL_TEXT_MAX_BYTES", 25 * 1024 * 1024))
        self.max_chars = int(getattr(config, "FULL_TEXT_MAX_CHARS", 400_000))
        self.timeout = float(getattr(config, "RESOLUTION_TIMEOUT_SECONDS", 35.0))

    # -- location discovery -------------------------------------------
    def unpaywall_location(self, doi: str) -> dict | None:
        if not getattr(self.config, "ENABLE_UNPAYWALL", False):
            return None
        email = getattr(self.config, "UNPAYWALL_EMAIL", None)
        if not email:
            return None
        url = f"https://api.unpaywall.org/v2/{quote(doi, safe='')}"

        def invoke():
            response = self.session.get(
                url, params={"email": email}, timeout=(10, self.timeout)
            )
            response.raise_for_status()
            return response.json()

        try:
            import json

            raw = self.recorder.call(
                provider="unpaywall",
                model="oa-api",
                purpose="full_text_location",
                prompt_version=FULL_TEXT_VERSION,
                request={"url": url, "params": {"email": "[configured]"}},
                invoke=invoke,
                decode=lambda value: (json.dumps(value, ensure_ascii=False), None),
            )
            document = json.loads(raw)
        except ProviderFailure as error:
            logger.info("Unpaywall lookup unavailable: %s", error.code)
            return None
        best = document.get("best_oa_location") or {}
        target = _https_only(best.get("url_for_pdf")) or _https_only(best.get("url"))
        if not target:
            return None
        return {
            "url": target,
            "provider": "unpaywall",
            "license": best.get("license"),
            "is_pdf": bool(best.get("url_for_pdf")),
        }

    def candidate_locations(self, candidate: dict, external: dict) -> list[dict]:
        """Open locations already known from resolution, in preference order."""
        locations: list[dict] = []
        direct = _https_only(candidate.get("open_access_pdf_url"))
        if direct:
            locations.append(
                {"url": direct, "provider": "semantic_scholar", "license": None, "is_pdf": True}
            )
        identifiers = candidate.get("external_ids") or {}
        arxiv = identifiers.get("ArXiv")
        if arxiv and re.fullmatch(r"[0-9]{4}\.[0-9]{4,5}(v[0-9]+)?", str(arxiv)):
            locations.append(
                {
                    "url": f"https://arxiv.org/pdf/{arxiv}",
                    "provider": "arxiv",
                    "license": "arxiv-nonexclusive",
                    "is_pdf": True,
                }
            )
        pmc = identifiers.get("PubMedCentral")
        if pmc and re.fullmatch(r"PMC[0-9]+", str(pmc)):
            locations.append(
                {
                    "url": (
                        "https://www.ncbi.nlm.nih.gov/pmc/articles/"
                        f"{pmc}/pdf/"
                    ),
                    "provider": "pmc",
                    "license": "pmc-open-access",
                    "is_pdf": True,
                }
            )
        oa_url = _https_only((external or {}).get("open_access_url"))
        if oa_url:
            locations.append(
                {"url": oa_url, "provider": "openalex", "license": None, "is_pdf": False}
            )
        return locations

    # -- retrieval ----------------------------------------------------
    def fetch(self, location: dict) -> FullTextResult | None:
        url = location["url"]
        try:
            response = self.session.get(
                url, timeout=(10, self.timeout), stream=True, allow_redirects=True
            )
            response.raise_for_status()
            final = urlparse(response.url)
            if final.scheme != "https":
                logger.info("Refusing a non-HTTPS redirect while acquiring full text.")
                return None
            payload = bytearray()
            for chunk in response.iter_content(64 * 1024):
                payload.extend(chunk)
                if len(payload) > self.max_bytes:
                    logger.info("Full text exceeded the configured size limit; skipping.")
                    return None
        except Exception as error:  # noqa: BLE001 - retrieval is best effort
            logger.info("Full-text retrieval failed for %s: %s", url, type(error).__name__)
            return None
        finally:
            try:
                response.close()  # type: ignore[possibly-undefined]
            except Exception:  # noqa: BLE001
                pass

        payload = bytes(payload)
        content_type = str(response.headers.get("Content-Type", "")).lower()
        try:
            if payload.startswith(PDF_MAGIC) or "pdf" in content_type:
                text = extract_pdf_text(payload, max_chars=self.max_chars)
            elif "html" in content_type or "xml" in content_type:
                from bs4 import BeautifulSoup

                soup = BeautifulSoup(payload.decode("utf-8", "replace"), "html.parser")
                for tag in soup(["script", "style", "nav", "footer", "header"]):
                    tag.decompose()
                text = soup.get_text(" ", strip=True)
            else:
                return None
        except Exception as error:  # noqa: BLE001
            logger.info("Could not extract text from %s: %s", url, type(error).__name__)
            return None

        text = re.sub(r"[ \t]{2,}", " ", text or "").strip()
        if len(text) < 500:
            # Too short to be an article body; the abstract remains better evidence.
            return None
        text, trimmed = strip_reference_section(text)
        truncated = len(text) > self.max_chars
        if truncated:
            text = text[: self.max_chars]
        return FullTextResult(
            text=text,
            kind="full_text",
            provider=location.get("provider", "unknown"),
            url=response.url,
            license=location.get("license"),
            truncated=truncated or trimmed,
        )

    def acquire(self, candidate: dict, external: dict) -> dict | None:
        """Return a full-text snapshot, or None to keep the abstract snapshot."""
        if not getattr(self.config, "ENABLE_FULL_TEXT_ACQUISITION", False):
            return None
        locations = self.candidate_locations(candidate, external)
        doi = normalize_doi((external or {}).get("doi") or candidate.get("doi")).strip()
        if doi:
            unpaywall = self.unpaywall_location(doi)
            if unpaywall:
                locations.append(unpaywall)
        for location in locations:
            result = self.fetch(location)
            if result is not None:
                return result.snapshot(
                    candidate_id=candidate.get("candidate_id") or candidate.get("id"),
                    doi=doi or None,
                )
        return None


__all__ = [
    "FULL_TEXT_VERSION",
    "FullTextAcquirer",
    "FullTextResult",
    "extract_pdf_text",
    "strip_reference_section",
]
