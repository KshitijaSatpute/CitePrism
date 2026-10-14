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


def auroc(pairs: list[tuple[float, bool]]) -> float | None:
    """Area under the ROC curve via the Mann-Whitney U statistic (ties count half).

    Threshold-free: how often a positive example outscores a negative one.
    Returns None when either class is absent, because the area is undefined.
    """
    positives = [score for score, label in pairs if label]
    negatives = [score for score, label in pairs if not label]
    if not positives or not negatives:
        return None
    ranked = sorted([(score, 1) for score in positives] + [(score, 0) for score in negatives])
    rank_sum, index = 0.0, 0
    while index < len(ranked):
        end = index
        while end + 1 < len(ranked) and ranked[end + 1][0] == ranked[index][0]:
            end += 1
        average_rank = (index + end) / 2 + 1
        rank_sum += average_rank * sum(1 for item in ranked[index:end + 1] if item[1])
        index = end + 1
    u = rank_sum - len(positives) * (len(positives) + 1) / 2
    return round(u / (len(positives) * len(negatives)), 4)


def threshold_sweep(pairs: list[tuple[float, bool]], *, lower_is_positive: bool = True) -> dict:
    """Find the cut-off that maximizes F1 for a score-based detector.

    With `lower_is_positive` a reference is flagged when its score is *below*
    the threshold — the shape of "potentially irrelevant if RS_final < t".
    """
    if not pairs:
        return {"samples": 0, "best": None, "curve": []}
    candidates = sorted({round(score, 2) for score, _ in pairs})
    candidates.append(candidates[-1] + 0.01)
    curve = []
    for threshold in candidates:
        predicted = [(label, (score < threshold) if lower_is_positive else (score >= threshold))
                     for score, label in pairs]
        report = binary_report(predicted)
        curve.append({"threshold": threshold, "precision": report["precision"],
                      "recall": report["recall"], "f1": report["f1"]})
    best = max(curve, key=lambda row: (row["f1"], row["precision"]))
    return {"samples": len(pairs), "best": best, "curve": curve,
            "direction": "flag_below" if lower_is_positive else "flag_at_or_above"}


def cross_validated_threshold(groups: list[list[tuple[float, bool]]], *, folds: int = 5,
                              seed: int = 20260101, lower_is_positive: bool = True) -> dict | None:
    """Choose a cut-off without grading it on the data that chose it.

    `groups` holds one list of (score, label) pairs per manuscript. Manuscripts
    are dealt into folds; for each fold the F1-optimal threshold is found on the
    other folds and applied to the held-out one. Pooling the held-out decisions
    gives an honest estimate of how the tuned threshold performs on unseen
    papers. The threshold to deploy is then tuned on all data, and the spread of
    per-fold thresholds shows how stable that choice is.
    """
    import random

    groups = [group for group in groups if group]
    if len(groups) < 2:
        return None
    folds = max(2, min(folds, len(groups)))
    order = list(range(len(groups)))
    random.Random(seed).shuffle(order)
    assignment = {index: position % folds for position, index in enumerate(order)}
    held_out_decisions: list[tuple[bool, bool]] = []
    per_fold = []
    for fold in range(folds):
        train = [pair for index, group in enumerate(groups) if assignment[index] != fold for pair in group]
        test = [pair for index, group in enumerate(groups) if assignment[index] == fold for pair in group]
        if not train or not test:
            continue
        best = threshold_sweep(train, lower_is_positive=lower_is_positive)["best"]
        if best is None:
            continue
        cut = best["threshold"]
        decisions = [(label, (score < cut) if lower_is_positive else (score >= cut)) for score, label in test]
        held_out_decisions.extend(decisions)
        per_fold.append({"fold": fold, "threshold": cut, "held_out": binary_report(decisions)})
    if not per_fold:
        return None
    final = threshold_sweep([pair for group in groups for pair in group], lower_is_positive=lower_is_positive)["best"]
    cuts = [row["threshold"] for row in per_fold]
    pooled = binary_report(held_out_decisions)
    return {
        "folds": len(per_fold),
        "manuscripts": len(groups),
        "held_out": {key: pooled[key] for key in ("samples", "precision", "recall", "f1")},
        "recommended_threshold": final["threshold"] if final else None,
        "in_sample_f1": final["f1"] if final else None,
        "fold_thresholds": {"min": min(cuts), "max": max(cuts), "values": cuts},
        "per_fold": per_fold,
        "note": "Held-out F1 is the figure to report; in-sample F1 is optimistic by construction.",
    }


def bootstrap_interval(groups: list, statistic, *, resamples: int = 1000, seed: int = 20260101,
                       confidence: float = 0.95) -> dict | None:
    """Percentile bootstrap over *groups* (manuscripts), not individual references.

    References within one manuscript are correlated; resampling them
    independently would report intervals that are too narrow. Resampling whole
    manuscripts respects that structure. Deterministic for a given seed.
    """
    import random

    if len(groups) < 2:
        return None
    rng = random.Random(seed)
    point = statistic(groups)
    values = []
    for _ in range(max(100, resamples)):
        sample = [groups[rng.randrange(len(groups))] for _ in groups]
        value = statistic(sample)
        if value is not None:
            values.append(value)
    if not values:
        return None
    values.sort()
    tail = (1 - confidence) / 2
    low = values[int(tail * (len(values) - 1))]
    high = values[int((1 - tail) * (len(values) - 1))]
    return {"point": point, "low": round(low, 4), "high": round(high, 4),
            "confidence": confidence, "resamples": len(values), "unit": "manuscript"}


__all__ = [
    "auroc",
    "bootstrap_interval",
    "threshold_sweep",
    "ClassMetrics",
    "agreement",
    "binary_report",
    "brier_score",
    "calibration_bins",
    "classification_report",
    "expected_calibration_error",
    "maximum_calibration_error",
]
