"""Hybrid citation relevance: grounded LLM judgment plus topic similarity.

CitePrism V2.0 specifies "hybrid relevance scoring using LLM judgment and
sentence embeddings" with ``RS_final = 0.6 × RS_llm + 0.4 × RS_embed``. Phase 2
correctly stopped treating any number as a verdict, but in doing so it stopped
computing relevance at all. This module restores the signal without giving up
the Phase 2 evidence rules:

* ``RS_llm`` is derived **only** from assessments whose claim and source
  quotations were located verbatim (``grounded_proposal``). An ungrounded model
  answer contributes nothing.
* ``insufficient_evidence`` contributes nothing either: "the abstract cannot
  tell" is a statement about the evidence available, not about relevance.
* ``RS_embed`` is the normalized topic similarity from `similarity.py`.
* When only one component exists the score is reported with that basis, and a
  reference is never labelled *potentially irrelevant* on topic similarity
  alone — distant topics are normal for methods and tooling citations.

Labels use the configured thresholds (``RELEVANT_THRESHOLD`` and
``BORDERLINE_THRESHOLD``). They are uncalibrated defaults until the evaluation
harness has been run on labelled data; every payload says so.
"""

from __future__ import annotations

from collections import Counter

RELEVANCE_VERSION = "hybrid-relevance-v2"

# How much a single grounded decision says about relevance (not about support).
# A contradicting source is highly relevant: it addresses the claim directly.
DECISION_RELEVANCE = {
    "supports": 1.0,
    "contradicts": 1.0,
    "context_only": 0.5,
    "unrelated": 0.0,
}

LABELS = ("relevant", "borderline", "potentially_irrelevant", "topic_only", "low_topical_similarity", "not_scored")
# Below this grounded model relevance the model itself did not find the source on-topic.
ON_TOPIC = 50.0


def llm_component(assessments: list[dict]) -> tuple[float | None, dict]:
    """Mean grounded relevance across this reference's citation uses (0-100)."""
    decisions = Counter()
    values = []
    for item in assessments or []:
        if item.get("status") != "grounded_proposal":
            continue
        decision = item.get("decision")
        decisions[decision] += 1
        if decision in DECISION_RELEVANCE:
            values.append(DECISION_RELEVANCE[decision])
    score = round(100.0 * sum(values) / len(values), 2) if values else None
    return score, {"grounded_uses": sum(decisions.values()), "scored_uses": len(values),
                   "decisions": dict(decisions)}


def weights(config) -> tuple[float, float]:
    llm = float(getattr(config, "LLM_WEIGHT", 0.6))
    embedding = float(getattr(config, "EMBEDDING_WEIGHT", 0.4))
    total = llm + embedding
    if total <= 0:
        return 0.6, 0.4
    return llm / total, embedding / total


def label_for(score: float | None, config) -> str:
    if score is None:
        return "not_scored"
    if score >= float(getattr(config, "RELEVANT_THRESHOLD", 70)):
        return "relevant"
    if score >= float(getattr(config, "BORDERLINE_THRESHOLD", 40)):
        return "borderline"
    return "potentially_irrelevant"


def assess_relevance(assessments: list[dict], similarity: dict | None, config) -> dict:
    """Combine both components into one explainable relevance record."""
    rs_llm, detail = llm_component(assessments)
    rs_embed = None if not similarity else similarity.get("score")
    w_llm, w_embed = weights(config)

    if rs_llm is not None and rs_embed is not None:
        final, basis = w_llm * rs_llm + w_embed * rs_embed, "hybrid"
    elif rs_llm is not None:
        final, basis = rs_llm, "llm_only"
    elif rs_embed is not None:
        final, basis = rs_embed, "embedding_only"
    else:
        final, basis = None, "none"

    final = None if final is None else round(final, 2)
    label = label_for(final, config)
    if basis == "embedding_only":
        # Topic similarity alone establishes neither irrelevance (methods and tools
        # are often off-topic) nor relevance: when the model checks did not run, a
        # similar topic must not read as "relevant".
        label = "low_topical_similarity" if label == "potentially_irrelevant" else "topic_only"
    elif basis == "hybrid" and label == "potentially_irrelevant" and rs_llm is not None and rs_llm >= ON_TOPIC:
        # The model read the source as on-topic (background or support); a distant
        # topic score lowers the number but does not make the citation irrelevant.
        label = "borderline"

    unrelated_uses = detail["decisions"].get("unrelated", 0)
    return {
        "method_version": RELEVANCE_VERSION,
        "RS_llm": rs_llm,
        "RS_embed": rs_embed,
        "RS_final": final,
        "label": label,
        "basis": basis,
        "weights": {"llm": round(w_llm, 3), "embedding": round(w_embed, 3)},
        "thresholds": {
            "relevant": float(getattr(config, "RELEVANT_THRESHOLD", 70)),
            "borderline": float(getattr(config, "BORDERLINE_THRESHOLD", 40)),
        },
        "similarity": similarity,
        "assessment": detail,
        "unrelated_uses": unrelated_uses,
        "calibration": "uncalibrated_defaults",
        "note": (
            "RS_llm uses only quotation-grounded model proposals; RS_embed is topic "
            "similarity. Thresholds are defaults until validated on labelled data."
        ),
    }


def needs_relevance_review(relevance: dict | None) -> bool:
    """Whether the review queue should raise a potential-irrelevance item."""
    if not relevance:
        return False
    if relevance.get("unrelated_uses"):
        return True
    if relevance.get("RS_llm") is not None and relevance["RS_llm"] >= ON_TOPIC:
        return False
    return relevance.get("label") == "potentially_irrelevant" and relevance.get("basis") in {
        "hybrid",
        "llm_only",
    }


__all__ = [
    "DECISION_RELEVANCE",
    "LABELS",
    "RELEVANCE_VERSION",
    "assess_relevance",
    "label_for",
    "llm_component",
    "needs_relevance_review",
    "weights",
]
