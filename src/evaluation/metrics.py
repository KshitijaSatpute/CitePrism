"""Classification and calibration metrics.

Calibration is the point of this module. A system that says "resolved" and is
right 60% of the time is worse than useless to a reviewer who reads "resolved"
as certainty, and accuracy alone hides that. Expected calibration error and a
reliability table make the gap visible.

Everything is pure Python so the metrics run in CI without a numeric stack.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


@dataclass
class ClassMetrics:
    label: str
    support: int
    predicted: int
    true_positive: int
    precision: float
    recall: float
    f1: float

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "support": self.support,
            "predicted": self.predicted,
            "true_positive": self.true_positive,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
        }


def _safe_divide(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def classification_report(pairs: list[tuple[str, str]]) -> dict:
    """Per-label precision/recall/F1 from (truth, prediction) pairs."""
    truths = Counter(truth for truth, _ in pairs)
    predictions = Counter(prediction for _, prediction in pairs)
    hits = Counter(truth for truth, prediction in pairs if truth == prediction)
    labels = sorted(set(truths) | set(predictions))

    per_label = []
    for label in labels:
        precision = _safe_divide(hits[label], predictions[label])
        recall = _safe_divide(hits[label], truths[label])
        f1 = _safe_divide(2 * precision * recall, precision + recall)
        per_label.append(
            ClassMetrics(label, truths[label], predictions[label], hits[label],
                         precision, recall, f1)
        )

    total = len(pairs)
    accuracy = _safe_divide(sum(hits.values()), total)
    macro_f1 = _safe_divide(sum(item.f1 for item in per_label), len(per_label))
    weighted_f1 = _safe_divide(
        sum(item.f1 * item.support for item in per_label), total
    )
    confusion: dict[str, dict[str, int]] = {label: {inner: 0 for inner in labels} for label in labels}
    for truth, prediction in pairs:
        confusion[truth][prediction] += 1

    return {
        "samples": total,
        "accuracy": accuracy,
        "macro_f1": macro_f1,
        "weighted_f1": weighted_f1,
        "per_label": [item.as_dict() for item in per_label],
        "confusion_matrix": confusion,
    }


def binary_report(pairs: list[tuple[bool, bool]]) -> dict:
    """Precision/recall for a yes-or-no decision such as 'surface for review'."""
    true_positive = sum(1 for truth, prediction in pairs if truth and prediction)
    false_positive = sum(1 for truth, prediction in pairs if not truth and prediction)
    false_negative = sum(1 for truth, prediction in pairs if truth and not prediction)
    true_negative = sum(1 for truth, prediction in pairs if not truth and not prediction)
    precision = _safe_divide(true_positive, true_positive + false_positive)
    recall = _safe_divide(true_positive, true_positive + false_negative)
    return {
        "samples": len(pairs),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "true_negative": true_negative,
        "precision": precision,
        "recall": recall,
        "f1": _safe_divide(2 * precision * recall, precision + recall),
        "accuracy": _safe_divide(true_positive + true_negative, len(pairs)),
    }


def brier_score(pairs: list[tuple[float, bool]]) -> float:
    """Mean squared error between a stated confidence and the outcome."""
    if not pairs:
        return 0.0
    total = sum((float(confidence) - (1.0 if outcome else 0.0)) ** 2 for confidence, outcome in pairs)
    return round(total / len(pairs), 6)


def calibration_bins(pairs: list[tuple[float, bool]], *, bins: int = 10) -> list[dict]:
    """Reliability table: stated confidence against observed frequency."""
    bins = max(2, min(100, int(bins)))
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for confidence, outcome in pairs:
        value = min(1.0, max(0.0, float(confidence)))
        index = min(bins - 1, int(value * bins))
        buckets[index].append((value, outcome))
    table = []
    for index, bucket in enumerate(buckets):
        lower, upper = index / bins, (index + 1) / bins
        count = len(bucket)
        mean_confidence = round(sum(item[0] for item in bucket) / count, 4) if count else 0.0
        observed = round(sum(1 for item in bucket if item[1]) / count, 4) if count else 0.0
        table.append(
            {
                "bin": index,
                "lower": round(lower, 4),
                "upper": round(upper, 4),
                "count": count,
                "mean_confidence": mean_confidence,
                "observed_frequency": observed,
                "gap": round(mean_confidence - observed, 4) if count else 0.0,
            }
        )
    return table


def expected_calibration_error(pairs: list[tuple[float, bool]], *, bins: int = 10) -> float:
    """Sample-weighted mean gap between stated confidence and reality."""
    if not pairs:
        return 0.0
    table = calibration_bins(pairs, bins=bins)
    total = len(pairs)
    error = sum(
        (row["count"] / total) * abs(row["mean_confidence"] - row["observed_frequency"])
        for row in table
        if row["count"]
    )
    return round(error, 6)


def maximum_calibration_error(pairs: list[tuple[float, bool]], *, bins: int = 10) -> float:
    if not pairs:
        return 0.0
    table = calibration_bins(pairs, bins=bins)
    gaps = [
        abs(row["mean_confidence"] - row["observed_frequency"]) for row in table if row["count"]
    ]
    return round(max(gaps), 6) if gaps else 0.0


def agreement(pairs: list[tuple[str, str]]) -> dict:
    """Cohen's kappa between two annotators labeling the same items."""
    if not pairs:
        return {"samples": 0, "observed_agreement": 0.0, "kappa": 0.0}
    total = len(pairs)
    observed = sum(1 for left, right in pairs if left == right) / total
    left_counts = Counter(left for left, _ in pairs)
    right_counts = Counter(right for _, right in pairs)
    expected = sum(
        (left_counts[label] / total) * (right_counts[label] / total)
        for label in set(left_counts) | set(right_counts)
    )
    kappa = (observed - expected) / (1 - expected) if expected < 1 else 1.0
    return {
        "samples": total,
        "observed_agreement": round(observed, 4),
        "expected_agreement": round(expected, 4),
        "kappa": round(kappa, 4),
    }


__all__ = [
    "ClassMetrics",
    "agreement",
    "binary_report",
    "brier_score",
    "calibration_bins",
    "classification_report",
    "expected_calibration_error",
    "maximum_calibration_error",
]
