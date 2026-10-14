"""Where a manuscript's text goes, and on what terms.

Reading a PDF, assessing citations and (optionally) the assistant send
manuscript text to language-model providers. Free plans may keep that text:
Google's Gemini API terms for unpaid services say content "is used to improve
products", "human reviewers may read" it, and "Do not submit sensitive,
confidential, or personal information" (terms updated 2026-04-28). A paper
under peer review is confidential by definition.

`CITEPRISM_MODEL_DATA_TERMS` records what the operator has arranged:

``unverified``  the default: CitePrism cannot know the plan behind a key, so the
                upload screen names the providers and warns against
                uploading confidential manuscripts;
``no_training`` the operator declares paid plans whose terms exclude training
                (e.g. a data-processing agreement); shown as such;
``local_only``  enforced: configuration is rejected unless every model that
                receives manuscript text runs on a local or private-network host.

Reference lookups (OpenAlex, Crossref, …) send titles and author names from the
reference list, never the manuscript's own text; they are named separately.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

TERMS = ("unverified", "no_training", "local_only")
PROVIDER_NAMES = {"google": "Google Gemini", "openai": "OpenAI"}
KNOWN_HOSTS = {"api.groq.com": "Groq", "api.openai.com": "OpenAI", "api.together.xyz": "Together AI",
               "api.mistral.ai": "Mistral AI", "openrouter.ai": "OpenRouter", "api.deepseek.com": "DeepSeek",
               "generativelanguage.googleapis.com": "Google Gemini"}
INDEX_NAMES = {"openalex": "OpenAlex", "crossref": "Crossref", "semantic_scholar": "Semantic Scholar",
               "dblp": "dblp", "europe_pmc": "Europe PMC", "datacite": "DataCite"}


def _address(host: str):
    """The IP address a host literal stands for, including integer forms ("3627734734"), or None."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if re.fullmatch(r"\d+", host):
        try:
            return ipaddress.ip_address(int(host))
        except ValueError:
            return None
    if re.fullmatch(r"0x[0-9a-f]+", host):
        try:
            return ipaddress.ip_address(int(host, 16))
        except ValueError:
            return None
    return None


def is_local_url(url: str | None) -> bool:
    """A loopback, private-network or single-label host name (e.g. a container name).

    IP literals are classified by address first, so "[2606:4700::1111]" or the
    integer form of a public address is never taken for a container name.
    """
    host = (urlparse(str(url or "")).hostname or "").lower().strip("[]")
    if not host:
        return False
    address = _address(host)
    if address is not None:
        return address.is_loopback or address.is_private or address.is_link_local
    if re.search(r"[^a-z0-9.\-]", host):
        return False
    return host == "localhost" or host.endswith((".local", ".internal", ".localhost")) or "." not in host


def _compatible_name(config) -> str:
    url = getattr(config, "OPENAI_COMPATIBLE_BASE_URL", None)
    if is_local_url(url):
        return "a local model"
    host = (urlparse(str(url or "")).hostname or "").lower()
    return KNOWN_HOSTS.get(host, host or "an OpenAI-compatible service")


def model_routes(config) -> list[tuple[str, str]]:
    """(use, provider) for every model that may receive manuscript text."""
    from src.evidence.providers import parse_fallback_chain

    routes = []
    primary = getattr(config, "LLM_PROVIDER", "google")
    providers = [primary]
    try:
        providers += [provider for provider, _ in parse_fallback_chain(getattr(config, "LLM_FALLBACK_MODELS", None),
                                                                        primary)]
    except ValueError:
        pass                                    # reported by configuration validation
    for provider in dict.fromkeys(providers):
        routes.append(("reading the PDF", provider))
    assessment = getattr(config, "ASSESSMENT_PROVIDER", "huggingface")
    routes.append(("checking citations", assessment))
    backend = getattr(config, "SIMILARITY_BACKEND", "auto")
    # "auto" falls back to the Hugging Face API when a token is set and no local model is
    # installed — except under local_only, where similarity.build_similarity skips it.
    may_use_api = backend == "auto" and getattr(config, "HF_API_TOKEN", None) and \
        not getattr(config, "OFFLINE_MODE", False) and getattr(config, "MODEL_DATA_TERMS", "") != "local_only"
    if backend == "hf_inference" or may_use_api:
        routes.append(("topic similarity", "huggingface"))
    return routes


def provider_label(config, provider: str) -> str:
    if provider == "openai_compatible":
        return _compatible_name(config)
    if provider == "huggingface":
        return "Hugging Face"
    return PROVIDER_NAMES.get(provider, provider)


def remote_routes(config) -> list[tuple[str, str]]:
    return [(use, provider) for use, provider in model_routes(config)
            if not (provider == "openai_compatible" and is_local_url(getattr(config, "OPENAI_COMPATIBLE_BASE_URL", None)))]


def policy_errors(config) -> list[str]:
    terms = getattr(config, "MODEL_DATA_TERMS", "unverified")
    if terms not in TERMS:
        return ["CITEPRISM_MODEL_DATA_TERMS must be unverified, no_training or local_only"]
    if terms != "local_only":
        return []
    errors = [f"CITEPRISM_MODEL_DATA_TERMS=local_only, but {use} uses {provider_label(config, provider)}; "
              "use LLM_PROVIDER/CITEPRISM_ASSESSMENT_PROVIDER=openai_compatible with a local "
              "OPENAI_COMPATIBLE_BASE_URL" for use, provider in remote_routes(config)]
    return list(dict.fromkeys(errors))


def data_handling(config) -> dict:
    """What the upload screen tells people about where manuscript text goes."""
    terms = getattr(config, "MODEL_DATA_TERMS", "unverified")
    if getattr(config, "OFFLINE_MODE", False):
        terms = "local_only"
    recipients = list(dict.fromkeys(provider_label(config, provider) for _, provider in model_routes(config)))
    indexes = [] if getattr(config, "OFFLINE_MODE", False) else [
        INDEX_NAMES.get(name, name) for name in getattr(config, "RESOLUTION_PROVIDERS", ())]
    return {"terms": terms if terms in TERMS else "unverified", "model_recipients": recipients,
            "reference_lookups": indexes}


__all__ = ["TERMS", "data_handling", "is_local_url", "model_routes", "policy_errors", "provider_label"]
