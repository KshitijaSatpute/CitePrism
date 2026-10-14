#!/usr/bin/env python
"""Calibrate the relevance threshold from human-labelled audits.

    python scripts/calibrate_thresholds.py \
        --dataset data/evaluation/gold_set.jsonl --artifacts data/scored

Prints the BORDERLINE_THRESHOLD that best separates citations annotators
labelled irrelevant, with the held-out (cross-validated) precision, recall and
F1 you can report, and the line to put in .env. It refuses to recommend a
value from too little data, because a threshold tuned on a handful of
citations looks precise and is not.

Thresholds only affect the analysis step, which is recomputed on every run
while the AI steps are reused, so re-running an audit applies a new threshold
without new AI calls.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MIN_MANUSCRIPTS = 10
MIN_POSITIVES = 20


def recommend(detector: dict | None, *, min_manuscripts: int = MIN_MANUSCRIPTS,
              min_positives: int = MIN_POSITIVES) -> dict:
    """Turn an evaluation's relevance detector section into a recommendation."""
    if not detector:
        return {"ok": False, "reason": "No labelled citation had a relevance score. Label relevance "
                                       "(relevant / borderline / irrelevant) in the gold set first."}
    cross = detector.get("cross_validated")
    problems = []
    if not cross or cross["manuscripts"] < min_manuscripts:
        problems.append(f"needs labels from at least {min_manuscripts} manuscripts "
                        f"(have {cross['manuscripts'] if cross else 0})")
    if detector["positives"] < min_positives:
        problems.append(f"needs at least {min_positives} citations labelled irrelevant (have {detector['positives']})")
    if problems:
        return {"ok": False, "reason": "Not enough labelled data: " + "; ".join(problems) + ".",
                "detector": detector}
    threshold = cross["recommended_threshold"]
    spread = cross["fold_thresholds"]["max"] - cross["fold_thresholds"]["min"]
    return {
        "ok": True,
        "borderline_threshold": round(float(threshold)),
        "held_out": cross["held_out"],
        "auroc": detector.get("auroc"),
        "fold_spread": round(spread, 2),
        "stable": spread <= 10,
        "env": f"BORDERLINE_THRESHOLD={round(float(threshold))}",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", required=True, help="labelled JSONL dataset")
    parser.add_argument("--artifacts", required=True, help="directory of scored artifacts")
    parser.add_argument("--min-manuscripts", type=int, default=MIN_MANUSCRIPTS)
    parser.add_argument("--min-positives", type=int, default=MIN_POSITIVES)
    parser.add_argument("--output", help="also write the recommendation as JSON")
    arguments = parser.parse_args(argv)

    from src.evaluation.dataset import load_dataset
    from src.evaluation.runner import evaluate_dataset

    source = Path(arguments.artifacts)
    paths = sorted(source.glob("*.json")) if source.is_dir() else [source]
    report = evaluate_dataset(load_dataset(arguments.dataset), paths)
    result = recommend(report["aggregate"].get("relevance_detector"),
                       min_manuscripts=arguments.min_manuscripts, min_positives=arguments.min_positives)
    if arguments.output:
        Path(arguments.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    if not result["ok"]:
        print(result["reason"])
        print("The default thresholds stay in place (RELEVANT_THRESHOLD=70, BORDERLINE_THRESHOLD=40).")
        return 1
    held = result["held_out"]
    print(f"Recommended: {result['env']}")
    print(f"Held-out performance (report this): precision {held['precision']}, recall {held['recall']}, "
          f"F1 {held['f1']} over {held['samples']} citations; AUROC {result['auroc']}.")
    print(f"Per-fold thresholds varied by {result['fold_spread']} points"
          + ("." if result["stable"] else " — unstable; label more manuscripts before relying on it."))
    print("Add the line to .env and restart CitePrism. Re-running an audit applies it without new AI calls.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
