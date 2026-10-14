"""Run the error-injection benchmark on a PDF and print the results.

    python scripts/benchmark_injected.py path/to/paper.pdf
    python scripts/benchmark_injected.py book.pdf --authors "Dana Blume" "Benno Volk" --json results.json

Known errors are planted in the document's own text and reference entries; the
numbers say how many CitePrism finds and how many false alarms it raises. It
needs no network, no model and no API key, and the same seed always gives the
same numbers. See src/evaluation/injection.py for what is and is not measured.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.evaluation.injection import run  # noqa: E402
from src.evidence.extraction import front_matter, pdf_source  # noqa: E402


def percent(value) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def markdown(report: dict, name: str) -> str:
    document = report["document"]
    lines = [f"## Error-injection benchmark — {name}", "",
             f"{document['pages']} pages, {document['references']} references, "
             f"{document['citation_occurrences']} in-text citations ({document['bibliography_style']}). "
             f"Seed {report['seed']}, {report['benchmark_version']}.", ""]
    typos = report.get("citation_year_typos")
    if typos:
        lines += ["**Citation year typos**", "",
                  f"- Planted: {typos['planted']} · found: {typos['detected']} (recall {percent(typos['recall'])})",
                  f"- Unexpected new conflicts: {typos['unexpected_new_conflicts']} "
                  f"(precision {percent(typos['precision'])})",
                  f"- Re-linked to another real entry: {typos['linked_to_another_entry']}", ""]
    dropped = report["dropped_citations"]
    lines += ["**Dropped citations** (an entry's every citation removed)", "",
              f"- Trials: {dropped['trials']} · entry reported as never cited: {dropped['detected']} "
              f"(recall {percent(dropped['recall'])})",
              f"- Other entries wrongly lost: {dropped['other_entries_lost']}", ""]
    lines += ["**Metadata differences** (published record perturbed)", "",
              "| Perturbation | Trials | Detected | Right severity | Other fields also flagged |",
              "|---|---|---|---|---|"]
    for perturbation, values in report["metadata_differences"]["perturbations"].items():
        if "no_difference_reported" in values:
            lines.append(f"| {perturbation} (should report nothing) | {values['trials']} | "
                         f"silent: {percent(values['no_difference_reported'])} | — | — |")
        else:
            lines.append(f"| {perturbation} | {values['trials']} | {percent(values['detected'])} | "
                         f"{percent(values['right_severity'])} | {percent(values['other_fields_also_flagged'])} |")
    identity = report["identity_resolution"]
    lines += ["", f"**Identity resolution** (right record among {identity['decoys_per_entry']} decoys, "
                  "same first author where possible; no DOI)", "",
              "| Case | Trials | Right | Wrong | Left for review |", "|---|---|---|---|---|"]
    for case, values in identity["cases"].items():
        lines.append(f"| {case} | {values['trials']} | {percent(values.get('right_match_rate'))} | "
                     f"{percent(values['wrong_match_rate'])} | {percent(values.get('left_for_review'))} |")
    self_citation = report.get("self_citation")
    if self_citation:
        lines += ["", "**Self-citation name matching**", "",
                  f"- Same person in {self_citation['same_person_trials']} written forms: found "
                  f"{percent(self_citation['same_person_found'])}",
                  f"- Different person (other initial, similar surname), {self_citation['different_person_trials']} "
                  f"forms: flagged {percent(self_citation['different_person_flagged'])}"]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--authors", nargs="*", help="manuscript authors (default: read from the first pages)")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--json", type=Path, help="also write the full results as JSON")
    arguments = parser.parse_args()
    source = pdf_source(arguments.pdf)
    authors = arguments.authors if arguments.authors is not None else (front_matter(source).get("authors") or [])
    report = run(source, manuscript_authors=authors, seed=arguments.seed, rounds=arguments.rounds)
    if arguments.json:
        arguments.json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(markdown(report, arguments.pdf.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
