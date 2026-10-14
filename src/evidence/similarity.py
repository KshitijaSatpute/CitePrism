"""Topic similarity between a manuscript and its cited sources.

This is the "sentence embeddings" half of CitePrism's hybrid relevance signal.
Three interchangeable backends are provided so the signal exists in every
deployment profile:

* ``sentence_transformers`` — a local embedding model (default
  ``all-MiniLM-L6-v2``). Best quality; needs the model weights on disk.
* ``hf_inference`` — the same family of models through the Hugging Face
  Inference API. No local model weights; needs a token and network access.
* ``lexical`` — TF-IDF cosine computed over the audit's own documents. Pure
  Python, deterministic, offline and instant. It measures shared vocabulary,
  not meaning, and every result produced with it is labelled as such.

``auto`` picks the first backend that is actually usable, in that order.

Raw cosine values are not comparable across backends (an unrelated pair scores
about 0.1 with MiniLM but about 0.01 with TF-IDF). Each backend therefore
declares a floor and a ceiling used to map its raw value onto 0–100. These are
documented starting points, not calibrated constants; the evaluation harness
reports how well they separate relevant from irrelevant references.
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

logger = logging.getLogger(__name__)

SIMILARITY_VERSION = "topic-similarity-v2"

STOPWORDS = frozenset(
    """a about above after again against all also am an and any are as at be because been
    before being below between both but by can could did do does doing down during each
    et al few for from further had has have having he her here hers herself him himself
    his how however i if in into is it its itself just me more most my myself no nor not
    now of off on once only or other our ours ourselves out over own same she should so
    some such than that the their theirs them themselves then there these they this those
    through to too under until up use used using very was we were what when where which
    while who whom why will with would you your yours yourself yourselves study paper
    results result method methods approach proposed show shows shown based new two one
    three may also however thus therefore within without via among across many much""".split()
)
TOKEN = re.compile(r"[^\W\d_][\w-]{2,}", re.UNICODE)

# (floor, ceiling) on raw cosine. Below the floor maps to 0, above the ceiling to 100.
DEFAULT_RANGES = {
    "sentence_transformers": (0.15, 0.60),
    "hf_inference": (0.15, 0.60),
    "lexical": (0.02, 0.25),
}


def tokens(text: str) -> list[str]:
    return [
        token
        for token in (match.group(0).casefold().strip("-") for match in TOKEN.finditer(text or ""))
        if token and token not in STOPWORDS
    ]


def cosine(left, right) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


@dataclass
class SimilarityResult:
    raw: float | None
    score: float | None  # normalized 0-100
    backend: str
    model: str | None

    def as_dict(self) -> dict:
        return {
            "raw_cosine": None if self.raw is None else round(self.raw, 4),
            "score": None if self.score is None else round(self.score, 2),
            "backend": self.backend,
            "model": self.model,
            "kind": "lexical_overlap" if self.backend == "lexical" else "semantic_embedding",
        }


class SimilarityBackend:
    name = "base"
    model: str | None = None

    def __init__(self, floor: float | None = None, ceiling: float | None = None):
        default_floor, default_ceiling = DEFAULT_RANGES.get(self.name, (0.0, 1.0))
        self.floor = default_floor if floor is None else float(floor)
        self.ceiling = default_ceiling if ceiling is None else float(ceiling)
        if self.ceiling <= self.floor:
            raise ValueError("similarity ceiling must exceed its floor")

    def normalize(self, raw: float | None) -> float | None:
        if raw is None or math.isnan(raw):
            return None
        scaled = (raw - self.floor) / (self.ceiling - self.floor)
        return max(0.0, min(1.0, scaled)) * 100.0

    def raw_similarities(self, query: str, documents: list[str]) -> list[float | None]:
        raise NotImplementedError

    def similarities(self, query: str, documents: list[str]) -> list[SimilarityResult]:
        if not query or not query.strip():
            return [SimilarityResult(None, None, self.name, self.model) for _ in documents]
        try:
            raws = self.raw_similarities(query, documents)
        except Exception as error:  # noqa: BLE001 - a diagnostic must never fail scoring
            logger.warning("Topic similarity backend %s failed: %s", self.name, type(error).__name__)
            raws = [None] * len(documents)
        return [
            SimilarityResult(
                raw if raw is None else float(raw),
                self.normalize(raw),
                self.name,
                self.model,
            )
            for raw in raws
        ]


class LexicalBackend(SimilarityBackend):
    """TF-IDF cosine with sublinear term frequency over the audit's documents."""

    name = "lexical"
    model = "tfidf-sublinear-v1"

    def raw_similarities(self, query: str, documents: list[str]) -> list[float | None]:
        corpus = [tokens(query)] + [tokens(document or "") for document in documents]
        frequency = Counter()
        for document in corpus:
            frequency.update(set(document))
        count = len(corpus)
        idf = {term: math.log((1 + count) / (1 + df)) + 1.0 for term, df in frequency.items()}
        vocabulary = sorted(idf)
        index = {term: position for position, term in enumerate(vocabulary)}

        def vector(document):
            counts = Counter(document)
            values = [0.0] * len(vocabulary)
            for term, tf in counts.items():
                values[index[term]] = (1.0 + math.log(tf)) * idf[term]
            return values

        query_vector = vector(corpus[0])
        results: list[float | None] = []
        for document, text in zip(corpus[1:], documents):
            results.append(cosine(query_vector, vector(document)) if text and document else None)
        return results


@lru_cache(maxsize=2)
def _load_sentence_transformer(model_name: str):
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_name)


class SentenceTransformerBackend(SimilarityBackend):
    name = "sentence_transformers"

    def __init__(self, model: str = "all-MiniLM-L6-v2", **kwargs):
        super().__init__(**kwargs)
        self.model = model
        self._encoder = _load_sentence_transformer(model)

    def raw_similarities(self, query: str, documents: list[str]) -> list[float | None]:
        present = [index for index, document in enumerate(documents) if document]
        if not present:
            return [None] * len(documents)
        # One batched encode for every source: this is the "batched inference"
        # path, and it is an order of magnitude faster than per-pair encoding.
        vectors = self._encoder.encode(
            [query] + [documents[index] for index in present],
            convert_to_numpy=True,
            batch_size=32,
            show_progress_bar=False,
        )
        query_vector = vectors[0].tolist()
        results: list[float | None] = [None] * len(documents)
        for offset, index in enumerate(present, start=1):
            results[index] = cosine(query_vector, vectors[offset].tolist())
        return results


class HuggingFaceInferenceBackend(SimilarityBackend):
    name = "hf_inference"

    def __init__(self, token: str, model: str = "sentence-transformers/all-MiniLM-L6-v2", **kwargs):
        super().__init__(**kwargs)
        from huggingface_hub import InferenceClient

        self.model = model
        self._client = InferenceClient(model=model, token=token, timeout=60)

    def _embed(self, text: str):
        value = self._client.feature_extraction(text[:8000])
        vector = value.tolist() if hasattr(value, "tolist") else value
        # Some endpoints return token-level matrices; mean-pool them.
        if vector and isinstance(vector[0], list):
            width = len(vector[0])
            vector = [sum(row[i] for row in vector) / len(vector) for i in range(width)]
        return [float(item) for item in vector]

    def raw_similarities(self, query: str, documents: list[str]) -> list[float | None]:
        query_vector = self._embed(query)
        results: list[float | None] = []
        for document in documents:
            if not document:
                results.append(None)
                continue
            try:
                results.append(cosine(query_vector, self._embed(document)))
            except Exception:  # noqa: BLE001
                results.append(None)
        return results


def _float_env(name: str) -> float | None:
    raw = os.getenv(name)
    try:
        return float(raw) if raw not in (None, "") else None
    except ValueError:
        return None


def build_similarity(config) -> SimilarityBackend | None:
    """Return the configured backend, or None when similarity is switched off."""
    choice = str(getattr(config, "SIMILARITY_BACKEND", "auto") or "auto").strip().lower()
    if not getattr(config, "ENABLE_TOPIC_SIMILARITY", True) or choice == "off":
        return None
    bounds = {
        "floor": _float_env("CITEPRISM_SIMILARITY_FLOOR"),
        "ceiling": _float_env("CITEPRISM_SIMILARITY_CEILING"),
    }
    model = getattr(config, "EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    offline = bool(getattr(config, "OFFLINE_MODE", False))
    # Under CITEPRISM_MODEL_DATA_TERMS=local_only no manuscript text may leave the
    # machine, so "auto" never falls back to the Hugging Face API.
    local_only = getattr(config, "MODEL_DATA_TERMS", "unverified") == "local_only"
    order = (
        ["sentence_transformers", "lexical"] if choice == "auto" and local_only else
        ["sentence_transformers", "hf_inference", "lexical"] if choice == "auto" else [choice]
    )
    for name in order:
        try:
            if name == "sentence_transformers":
                return SentenceTransformerBackend(model, **bounds)
            if name == "hf_inference":
                token = getattr(config, "HF_API_TOKEN", None)
                if offline or not token:
                    if choice != "auto":
                        logger.warning("hf_inference similarity needs a token and network access.")
                    continue
                hf_model = model if "/" in model else f"sentence-transformers/{model}"
                return HuggingFaceInferenceBackend(token, hf_model, **bounds)
            if name == "lexical":
                return LexicalBackend(**bounds)
        except Exception as error:  # noqa: BLE001 - fall through to the next backend
            logger.info("Similarity backend %s unavailable: %s", name, type(error).__name__)
    logger.warning("No topic similarity backend is available; relevance uses LLM evidence only.")
    return None


__all__ = [
    "DEFAULT_RANGES",
    "HuggingFaceInferenceBackend",
    "LexicalBackend",
    "SIMILARITY_VERSION",
    "SentenceTransformerBackend",
    "SimilarityResult",
    "build_similarity",
    "cosine",
    "tokens",
]
