"""Select the parts of a long source that a citation is most likely about.

A full paper is far longer than an assessment prompt can carry, and the first
16,000 characters of a paper are mostly its introduction. Sending only those to
the model and asking whether a specific claim is supported quietly turns
"full-text evidence" back into "abstract-and-introduction evidence".

This module splits the source into overlapping windows, ranks them by lexical
relevance to the citing sentence, and returns the abstract followed by the best
windows, within a character budget. Every window is a verbatim substring of the
stored source, so quotations taken from it still validate against the snapshot.
Ranking is deterministic, so the same audit always sends the same evidence.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from src.evidence.similarity import tokens

PASSAGE_VERSION = "passages-v1"
SEPARATOR = "\n\n[...]\n\n"


@dataclass(frozen=True)
class Passage:
    start: int
    end: int
    score: float

    def as_dict(self) -> dict:
        return {"char_start": self.start, "char_end": self.end, "score": round(self.score, 4)}


def windows(text: str, size: int = 1400, overlap: int = 300) -> list[tuple[int, int]]:
    if size <= overlap:
        raise ValueError("window size must exceed the overlap")
    spans, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        # Prefer to end at a sentence boundary so quotations are not cut mid-way.
        boundary = text.rfind(". ", start + size // 2, end)
        if end < len(text) and boundary != -1:
            end = boundary + 1
        spans.append((start, end))
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return spans


def rank(text: str, query: str, spans: list[tuple[int, int]]) -> list[Passage]:
    query_terms = Counter(tokens(query))
    if not query_terms:
        return [Passage(start, end, 0.0) for start, end in spans]
    documents = [Counter(tokens(text[start:end])) for start, end in spans]
    document_frequency = Counter()
    for document in documents:
        document_frequency.update(set(document))
    total = max(1, len(documents))
    average_length = sum(sum(d.values()) for d in documents) / total or 1.0
    ranked = []
    # Okapi BM25 with the conventional k1/b; robust and parameter-light.
    k1, b = 1.4, 0.75
    for (start, end), document in zip(spans, documents):
        length = sum(document.values()) or 1
        score = 0.0
        for term in query_terms:
            frequency = document.get(term, 0)
            if not frequency:
                continue
            idf = math.log(1 + (total - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
            score += idf * frequency * (k1 + 1) / (frequency + k1 * (1 - b + b * length / average_length))
        ranked.append(Passage(start, end, score))
    return ranked


def select_passages(
    text: str,
    query: str,
    *,
    abstract: str | None = None,
    budget: int = 16000,
    window_size: int = 1400,
    overlap: int = 300,
    top_k: int = 6,
) -> dict:
    """Build the evidence excerpt for one citation use.

    Returns the excerpt plus the exact spans used, so a reviewer can see which
    parts of the paper the model was shown.
    """
    text = text or ""
    if len(text) <= budget:
        return {"excerpt": text, "passages": [], "truncated": False, "method": "whole_source"}

    parts: list[str] = []
    used = 0
    if abstract and abstract.strip():
        head = abstract.strip()[: budget // 4]
        parts.append(head)
        used += len(head) + len(SEPARATOR)

    ranked = sorted(rank(text, query, windows(text, window_size, overlap)),
                    key=lambda passage: passage.score, reverse=True)
    chosen: list[Passage] = []
    for passage in ranked:
        if len(chosen) >= top_k:
            break
        if any(passage.start < other.end and other.start < passage.end for other in chosen):
            continue
        length = passage.end - passage.start
        if used + length > budget:
            continue
        chosen.append(passage)
        used += length + len(SEPARATOR)

    if not chosen:
        # No lexical signal at all: fall back to the opening of the paper.
        excerpt = SEPARATOR.join(parts + [text[: max(0, budget - used)]])
        return {"excerpt": excerpt, "passages": [], "truncated": True, "method": "leading_text"}

    chosen.sort(key=lambda passage: passage.start)  # keep document order for readability
    parts.extend(text[passage.start:passage.end] for passage in chosen)
    return {
        "excerpt": SEPARATOR.join(parts),
        "passages": [passage.as_dict() for passage in chosen],
        "truncated": True,
        "method": PASSAGE_VERSION,
    }


__all__ = ["PASSAGE_VERSION", "Passage", "rank", "select_passages", "windows"]
