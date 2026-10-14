#!/usr/bin/env python
"""Score CitePrism artifacts against a labeled dataset.

    python scripts/run_evaluation.py \
        --dataset data/evaluation/gold_set.jsonl \
        --artifacts data/scored \
        --output data/evaluation/report.json

Metrics describe agreement with these annotators on these manuscripts. They do
not establish that any proposal is scientifically correct.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a CitePrism evaluation")
    parser.add_argument("--dataset", required=True, help="labeled JSONL dataset")
    parser.add_argument("--artifacts", required=True,
                        help="directory of scored artifacts, or a single JSON file")
    parser.add_argument("--output", default=None, help="write the full report as JSON")
    parser.add_argument("--bins", type=int, default=10, help="calibration bins (default 10)")
    parser.add_argument("--markdown", default=None,
                        help="also write a publication-ready Markdown report with LaTeX tables")
    arguments = parser.parse_args(argv)

    from src.evaluation.dataset import load_dataset
    from src.evaluation.runner import evaluate_dataset

    source = Path(arguments.artifacts)
    paths = sorted(source.glob("*.json")) if source.is_dir() else [source]
    if not paths:
        parser.error(f"no artifacts were found in {source}")

    dataset = load_dataset(arguments.dataset)
    problems = dataset.validate()
    if problems:
        print("Dataset problems:")
        for problem in problems:
            print(f"  - {problem}")
        return 2

    report = evaluate_dataset(dataset, paths, bins=arguments.bins)

    print(f"Dataset: {report['dataset']['name']} "
          f"({report['dataset']['items']} items, "
          f"{report['dataset']['manuscripts']} manuscripts)")
    print(f"Evaluated: {report['manuscripts_evaluated']} manuscript(s)")
    if report["manuscripts_missing_artifact"]:
        print(f"Missing artifacts for {len(report['manuscripts_missing_artifact'])} manuscript(s)")

    for name in ("identity", "support", "extraction", "relevance"):
        section = report["aggregate"].get(name)
        if section:
            print(f"\n{name.title()} — accuracy {section['accuracy']}, "
                  f"macro F1 {section['macro_f1']} (n={section['samples']})")
            for label in section["per_label"]:
                print(f"   {label['label']:24s} P={label['precision']:<7} "
                      f"R={label['recall']:<7} F1={label['f1']:<7} n={label['support']}")

    triage = report["aggregate"].get("triage")
    if triage:
        print(f"\nTriage — precision {triage['precision']}, recall {triage['recall']} "
              f"(n={triage['samples']})")

    calibration = report["aggregate"].get("calibration")
    if calibration:
        print(f"\nCalibration of {calibration['signal']} (n={calibration['samples']})")
        print(f"   Brier score: {calibration['brier_score']}")
        print(f"   Expected calibration error: {calibration['expected_calibration_error']}")
        print(f"   Maximum calibration error: {calibration['maximum_calibration_error']}")
        print("   bin    n   stated   observed")
        for row in calibration["reliability"]:
            if row["count"]:
                print(f"   {row['lower']:.1f}-{row['upper']:.1f} {row['count']:>4} "
                      f"{row['mean_confidence']:>8} {row['observed_frequency']:>10}")

    for name in ("self_citation", "metadata"):
        section = report["aggregate"].get(name)
        if section:
            print(f"\n{name.replace('_', ' ').title()} — precision {section['precision']}, "
                  f"recall {section['recall']}, F1 {section['f1']} (n={section['samples']})")

    detector = report["aggregate"].get("relevance_detector")
    if detector:
        best = detector["recommended_threshold"] or {}
        print(f"\nIrrelevance detection from RS_final (n={detector['samples']}, "
              f"positives={detector['positives']}): AUROC {detector['auroc']}")
        print(f"   F1-optimal threshold {best.get('threshold')} -> P={best.get('precision')} "
              f"R={best.get('recall')} F1={best.get('f1')} (tune and report on separate splits)")

    if report.get("uncertainty"):
        print("\n95% manuscript-level bootstrap intervals")
        for name, interval in report["uncertainty"].items():
            if interval:
                print(f"   {name:22s} {interval['point']}  [{interval['low']}, {interval['high']}]")

    if report.get("domains"):
        print("\nBy domain")
        for domain, row in report["domains"].items():
            print(f"   {domain:22s} manuscripts={row['manuscripts']} "
                  f"identity_acc={row['identity_accuracy']} relevance_F1={row['relevance_macro_f1']}")

    print("\nLimitations")
    for item in report["limitations"]:
        print(f"  - {item}")

    if arguments.output:
        Path(arguments.output).parent.mkdir(parents=True, exist_ok=True)
        Path(arguments.output).write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nFull report written to {arguments.output}")
    if arguments.markdown:
        from src.evaluation.report import render_markdown

        Path(arguments.markdown).parent.mkdir(parents=True, exist_ok=True)
        Path(arguments.markdown).write_text(render_markdown(report), encoding="utf-8")
        print(f"Markdown report written to {arguments.markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
