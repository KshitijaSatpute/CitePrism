"""Render an evaluation report as Markdown with LaTeX tables.

The V2.0 publication goal needs results a paper can cite directly. Every table
carries its sample size and, where available, a manuscript-level bootstrap
interval, so a reader can judge how much a number is worth.
"""

from __future__ import annotations


def _fmt(value) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _interval(uncertainty: dict, name: str) -> str:
    interval = (uncertainty or {}).get(name)
    if not interval:
        return ""
    return f" [{interval['low']:.3f}, {interval['high']:.3f}]"


def _latex_table(caption: str, label: str, header: list[str], rows: list[list[str]]) -> str:
    columns = "l" + "r" * (len(header) - 1)
    body = "\n".join("    " + " & ".join(cell.replace("_", "\\_") for cell in row) + " \\\\" for row in rows)
    return (
        "```latex\n\\begin{table}[t]\n  \\centering\n"
        f"  \\caption{{{caption}}}\n  \\label{{{label}}}\n"
        f"  \\begin{{tabular}}{{{columns}}}\n    \\toprule\n"
        f"    {' & '.join(header)} \\\\\n    \\midrule\n{body}\n    \\bottomrule\n"
        "  \\end{tabular}\n\\end{table}\n```\n"
    )


def render_markdown(report: dict) -> str:
    dataset = report.get("dataset", {})
    aggregate = report.get("aggregate", {})
    uncertainty = report.get("uncertainty", {})
    lines = [
        f"# CitePrism evaluation — {dataset.get('name', 'dataset')}",
        "",
        f"- Labelled items: {dataset.get('items')} across {dataset.get('manuscripts')} manuscripts",
        f"- Manuscripts evaluated: {report.get('manuscripts_evaluated')}",
        f"- Annotators: {', '.join(dataset.get('annotators') or []) or 'unspecified'}",
        f"- Domains: {', '.join(dataset.get('domains') or []) or 'unspecified'}",
        "",
        "Intervals are 95% percentile bootstraps over manuscripts.",
        "",
        "## Headline metrics",
        "",
        "| Task | Metric | Value | n |",
        "|---|---|---|---|",
    ]
    rows = []
    for task, metric, key, interval in (
        ("identity", "accuracy", "accuracy", "identity_accuracy"),
        ("support", "macro F1", "macro_f1", "support_macro_f1"),
        ("relevance", "macro F1", "macro_f1", "relevance_macro_f1"),
        ("extraction", "accuracy", "accuracy", None),
    ):
        section = aggregate.get(task)
        if section:
            value = _fmt(section.get(key)) + (_interval(uncertainty, interval) if interval else "")
            lines.append(f"| {task} | {metric} | {value} | {section.get('samples')} |")
            rows.append([task, metric, value, str(section.get("samples"))])
    for task in ("triage", "self_citation", "metadata"):
        section = aggregate.get(task)
        if section:
            for metric in ("precision", "recall", "f1"):
                interval = f"{task}_{metric}" if task == "triage" and metric != "f1" else None
                value = _fmt(section.get(metric)) + (_interval(uncertainty, interval) if interval else "")
                lines.append(f"| {task} | {metric} | {value} | {section.get('samples')} |")
                rows.append([task, metric, value, str(section.get("samples"))])
    lines.append("")
    if rows:
        lines.append(_latex_table("CitePrism agreement with expert annotation.", "tab:citeprism-eval",
                                  ["Task", "Metric", "Value", "$n$"], rows))

    detector = aggregate.get("relevance_detector")
    if detector:
        best = detector.get("recommended_threshold") or {}
        lines += [
            "## Irrelevant-citation detection",
            "",
            f"- AUROC of RS_final: {_fmt(detector.get('auroc'))} (n={detector['samples']}, "
            f"positives={detector['positives']})",
            f"- F1-optimal threshold: {best.get('threshold')} → precision {_fmt(best.get('precision'))}, "
            f"recall {_fmt(best.get('recall'))}, F1 {_fmt(best.get('f1'))}",
            f"- Brier score of RS_final as P(relevant): {_fmt(detector['calibration']['brier_score'])}; "
            f"ECE {_fmt(detector['calibration']['expected_calibration_error'])}",
            "- The threshold was selected on this dataset; confirm it on held-out manuscripts.",
            "",
        ]

    calibration = aggregate.get("calibration")
    if calibration:
        lines += [
            "## Identity calibration",
            "",
            f"Signal: {calibration['signal']} (n={calibration['samples']}). "
            f"Brier {_fmt(calibration['brier_score'])}, ECE {_fmt(calibration['expected_calibration_error'])}"
            f"{_interval(uncertainty, 'identity_ece')}, MCE {_fmt(calibration['maximum_calibration_error'])}.",
            "",
            "| Bin | n | Mean stated | Observed |",
            "|---|---|---|---|",
        ]
        for row in calibration["reliability"]:
            if row["count"]:
                lines.append(f"| {row['lower']:.1f}–{row['upper']:.1f} | {row['count']} | "
                             f"{row['mean_confidence']:.3f} | {row['observed_frequency']:.3f} |")
        lines.append("")

    if report.get("domains"):
        lines += ["## By domain", "", "| Domain | Manuscripts | Identity acc. | Support acc. | Relevance F1 | Triage recall |",
                  "|---|---|---|---|---|---|"]
        for domain, row in report["domains"].items():
            lines.append(f"| {domain} | {row['manuscripts']} | {_fmt(row['identity_accuracy'])} | "
                         f"{_fmt(row['support_accuracy'])} | {_fmt(row['relevance_macro_f1'])} | "
                         f"{_fmt(row['triage_recall'])} |")
        lines.append("")

    agreement = report.get("annotator_agreement")
    if agreement:
        lines += ["## Inter-annotator agreement", "",
                  f"Cohen's κ on {agreement['signal']}: {_fmt(agreement['kappa'])} "
                  f"(n={agreement['samples']}, observed agreement {_fmt(agreement['observed_agreement'])}).", ""]

    lines += ["## Limitations", ""] + [f"- {item}" for item in report.get("limitations", [])] + [""]
    return "\n".join(lines)


__all__ = ["render_markdown"]
