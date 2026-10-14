"""The plain-language report: the audit as a document anyone can act on.

The review package (`src.workspace.exports.package_pdf`) is the formal record
for editors. This report is for the person who has to *do* something with the
results — often an author or student with no background in citation checking.
It explains what was checked, what was found, and turns every open item into a
checklist with concrete steps, followed by possibly missing literature, a
glossary and an honest statement of what the results can and cannot show.

Two registers, as in the assistant: ``standard`` and ``simple``. Everything is
built from the same review projection the workspace shows; nothing is added.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Optional

from src.assistant.content import GLOSSARY, HOW_IT_WORKS, PLAIN, SIMPLE, fix_steps
from src.assistant.engine import (PAPER_LEVEL, PAPER_LEVEL_SIMPLE, PRIORITY_ORDER, AuditAssistant, Opts, clip,
                                  display_title, plural)
from src.workspace.exports import ACCENT, CAUTION, GRAPHITE, WARNING, SimplePdf

REPORT_VERSION = "plain-report-v1"
MAX_PER_GROUP = 40
DISPOSITION_WORDS = {"confirm": "Confirmed", "dismiss": "Dismissed", "needs_more_evidence": "Needs more evidence",
                     "escalate": "Escalated"}
GLOSSARY_ORDER = ("citation", "reference", "doi", "metadata", "database", "resolved", "abstract", "relevance",
                  "grounded", "self_citation", "orcid", "cocitation", "retraction")


def _byline(ref: dict) -> str:
    authors = ref.get("authors") or []
    names = authors[0].split(",")[0] if authors else ""
    if len(authors) > 1:
        names += " et al."
    year = ref.get("year")
    return ", ".join(part for part in (names, str(year) if year else "") if part)


def _row(ref: dict) -> str:
    byline = _byline(ref)
    marker = str(ref.get("marker") or "")
    title = clip(display_title(ref.get("title")) or "Untitled entry", 160)
    if byline and byline.split(",")[0].split()[0].lower() in marker.lower():
        return f"{marker} — {title}"
    return f"{marker} — {title}" + (f" ({byline})" if byline else "")


class ReportBuilder:
    def __init__(self, review: dict, *, artifact: Optional[dict] = None, style: str = "standard",
                 manuscript: Optional[dict] = None, audit: Optional[dict] = None, generated_at: Optional[str] = None):
        self.review = review
        self.simple = style == "simple"
        self.opts = Opts(simple=self.simple, detailed=True)
        self.assistant = AuditAssistant(review, artifact=artifact)
        self.manuscript = manuscript or {}
        self.audit = audit or {}
        self.generated_at = generated_at
        self.findings = review.get("findings") or []
        self.by_id = {ref["id"]: ref for ref in review.get("references") or []}
        self.pdf = SimplePdf(("Your citation check, explained" if self.simple else "Citation check report")
                             + f" — {clip(review.get('title') or 'Untitled', 200)}",
                             footer="CitePrism plain-language report · pointers for a person to check, not verdicts")

    # ------------------------------------------------------------------ helpers
    def _label(self, finding: dict) -> tuple[str, str, str]:
        return self.assistant._plain(finding, self.simple)

    def _groups(self, findings: list[dict]) -> "OrderedDict[str, list[dict]]":
        groups: dict[str, list[dict]] = {}
        for finding in findings:
            key = finding["category"] + ("_high" if finding["category"] == "metadata_difference"
                                         and finding["priority"] == "high" else "")
            groups.setdefault(key, []).append(finding)
        ordered = sorted(groups.items(), key=lambda item: (min(PRIORITY_ORDER.get(f["priority"], 9) for f in item[1]),
                                                          -len(item[1])))
        return OrderedDict(ordered)

    def _priority_word(self, priority: str) -> str:
        if self.simple:
            return {"high": "Important", "moderate": "Worth a look", "low": "If you have time"}.get(priority, "")
        return {"high": "Priority 1 — serious", "moderate": "Priority 2 — worth a look",
                "low": "Priority 3 — optional"}.get(priority, "")

    # ------------------------------------------------------------------ sections
    def cover(self) -> None:
        pdf, review = self.pdf, self.review
        pdf.heading("Your citation check, explained" if self.simple else "Citation check report", level=1)
        pdf.text(clip(review.get("title") or "Untitled manuscript", 400), size=13, bold=True, gap=2)
        details = []
        if self.manuscript.get("original_filename"):
            details.append(str(self.manuscript["original_filename"]))
        if self.audit.get("completed_at"):
            details.append(f"checked {str(self.audit['completed_at'])[:10]}")
        if self.audit.get("id"):
            details.append(f"audit {str(self.audit['id'])[:8]}")
        if self.generated_at:
            details.append(f"report made {str(self.generated_at)[:10]}")
        if details:
            pdf.text(" · ".join(details), size=9, color=GRAPHITE, gap=6)
        overview = self.assistant._overview(Opts(simple=self.simple))
        pdf.callout(overview["text"] + " " + (
            "Everything below is a pointer for a person to check; nothing in it is a final verdict."
            if not self.simple else "CitePrism only points; a person decides."),
            title="In one minute" if not self.simple else "The short version")
        document_notes = set(PAPER_LEVEL.values()) | set(PAPER_LEVEL_SIMPLE.values())
        for point in overview["points"]:
            if point.startswith("Think of the reference list") or point.startswith("None of this") \
                    or point in document_notes or "recorded decision" in point or "a decision from a person" in point:
                continue  # document-level notes and decisions get their own lines
            pdf.bullet(point)
        decided = sum(bool(f.get("decision")) for f in self.findings)
        if decided:
            pdf.bullet(f"{plural(decided, 'item')} already {'has' if decided == 1 else 'have'} a reviewer's decision "
                       "(listed near the end).")
        pdf.spacer(4)

    def how(self) -> None:
        self.pdf.heading("How the check worked" if not self.simple else "How I checked", level=2)
        for index, step in enumerate(HOW_IT_WORKS["simple" if self.simple else "standard"], 1):
            self.pdf.numbered(index, step)

    def document_level(self) -> None:
        notes = [f for f in self.findings if not f.get("reference_id") and f["priority"] != "informational"]
        if not notes:
            return
        self.pdf.heading("About the document as a whole" if not self.simple else "About the whole document", level=2)
        table = PAPER_LEVEL_SIMPLE if self.simple else PAPER_LEVEL
        for finding in notes:
            tone = WARNING if finding["priority"] == "high" else CAUTION
            self.pdf.callout(table.get(finding["category"]) or finding["explanation"],
                             title=finding["summary"], tone=tone)
            self.pdf.text(("What to do: " if not self.simple else "What to do: ") + finding["action"], size=10, gap=4)

    def found(self) -> None:
        """One short explanation per kind of finding; the references themselves are in the to-do list."""
        pdf = self.pdf
        relevant = [f for f in self.findings if f.get("reference_id") in self.by_id and not f.get("system_issue")
                    and f["priority"] != "informational"]
        pdf.heading("What was found" if not self.simple else "What I found", level=2)
        if not relevant:
            pdf.text("Nothing about the references needs attention." if not self.simple
                     else "Nothing about the sources needs a second look.")
            return
        for key, findings in self._groups(relevant).items():
            label, meaning, _ = self._label(findings[0])
            refs = {f["reference_id"] for f in findings}
            decided = {f["reference_id"] for f in findings if f.get("decision")}
            pdf.text(f"{label} — {plural(len(refs), 'reference' if not self.simple else 'source')}"
                     + (f" ({len(decided)} already decided)" if decided else ""), bold=True, size=10.5)
            pdf.text(meaning, size=10, color=GRAPHITE, gap=5, indent=8)

    def todo(self) -> None:
        pdf = self.pdf
        open_findings = [f for f in self.findings if f.get("reference_id") in self.by_id and not f.get("system_issue")
                         and f["priority"] != "informational" and not f.get("decision")]
        pdf.heading("What to do, step by step" if not self.simple else "Your to-do list", level=2)
        if not open_findings:
            pdf.text("Every item has been decided. Nothing is left to do." if not self.simple
                     else "Everything has been looked at. Nothing left to do!")
            return
        pdf.text("Work from the top down: the most serious items come first. For each kind of item, follow the steps, "
                 "then tick the references you have dealt with and record your decision in CitePrism (Review in "
                 "detail → Items to check)." if not self.simple else
                 "Start at the top — the most important things come first. Follow the steps, then tick the box.",
                 gap=4)
        for key, findings in self._groups(open_findings).items():
            first = findings[0]
            label = self._label(first)[0]
            priority = min((f["priority"] for f in findings), key=lambda value: PRIORITY_ORDER.get(value, 9))
            color = WARNING if priority == "high" else CAUTION if priority == "moderate" else ACCENT
            pdf.heading(f"{self._priority_word(priority)}: {label}", level=3, color=color)
            steps = fix_steps(first["category"], priority=first["priority"], simple=self.simple)
            for index, step in enumerate(steps, 1):
                pdf.numbered(index, step, indent=4)
            pdf.spacer(2)
            refs = list(OrderedDict((f["reference_id"], f) for f in findings).items())
            previous = None
            for ref_id, finding in refs[:MAX_PER_GROUP]:
                ref = self.by_id[ref_id]
                if self.simple:
                    # Everyday words, but still the specifics: which year, which page, which score.
                    detail = self.assistant._plain_specific(finding)
                else:
                    detail = (self.assistant._detail(finding, self.opts, full=True)
                              if self.assistant._specific(finding) else None)
                shown = detail
                if detail and detail == previous:
                    shown = "Same as the entry above." if not self.simple else "Same as above."
                previous = detail
                pdf.checkbox(_row(ref), detail=shown, indent=4)
            if len(refs) > MAX_PER_GROUP:
                pdf.text(f"…and {len(refs) - MAX_PER_GROUP} more in Review in detail → Items to check.", size=9,
                         color=GRAPHITE, indent=4)

    def missing(self) -> None:
        section = self.assistant._missing(Opts(simple=self.simple, detailed=True))
        self.pdf.heading("Citations that may be missing" if not self.simple else "Works the paper might have missed",
                         level=2)
        self.pdf.text(section["text"], gap=2)
        for point in section["points"]:
            if point.startswith("How to decide: "):
                self.pdf.text("How to decide:" if not self.simple else "How to decide:", bold=True, size=10)
                for index, step in enumerate(fix_steps("missing_literature", simple=self.simple), 1):
                    self.pdf.numbered(index, step, indent=4)
            else:
                self.pdf.bullet(point)

    def self_citation(self) -> None:
        section = self.assistant._self_citation(Opts(simple=self.simple))
        if not self.review.get("counts", {}).get("self_citations") and not section["references"]:
            return
        self.pdf.heading("Self-citation" if not self.simple else "Authors citing themselves", level=2)
        self.pdf.text(section["text"], gap=2)
        for point in section["points"]:
            self.pdf.bullet(point)

    def technical(self) -> None:
        technical = [f for f in self.findings if f.get("system_issue")]
        if not technical:
            return
        pdf = self.pdf
        pdf.heading("Checks that did not finish" if not self.simple else "Things that didn't finish", level=2)
        pdf.text(f"{plural(len(technical), 'check')} did not finish because of technical problems on CitePrism's "
                 "side. They say nothing about the paper." if not self.simple else
                 f"{plural(len(technical), 'check')} didn't finish because something went wrong on our side. "
                 "That's not the paper's fault.", gap=2)
        for index, step in enumerate(fix_steps("technical", simple=self.simple), 1):
            pdf.numbered(index, step, indent=4)
        for finding in technical[:MAX_PER_GROUP]:
            ref = self.by_id.get(finding.get("reference_id"))
            reason = clip(finding["explanation"], 200) if not self.simple else self._label(finding)[0]
            pdf.bullet((_row(ref) + ": " if ref else "") + reason, size=9.5)

    def notes(self) -> None:
        informational = [f for f in self.findings if f["priority"] == "informational" and not f.get("system_issue")]
        if not informational:
            return
        counts: dict[str, int] = {}
        for finding in informational:
            label = (SIMPLE if self.simple else PLAIN).get(finding["category"], (finding["summary"],))[0]
            counts[label] = counts.get(label, 0) + 1
        self.pdf.heading("Notes for information (no action needed)" if not self.simple else "Small notes (nothing to do)",
                         level=2)
        for label, count in sorted(counts.items(), key=lambda item: -item[1]):
            self.pdf.bullet(f"{label}: {plural(count, 'reference' if not self.simple else 'source')}.")

    def decisions(self) -> None:
        decided = [f for f in self.findings if f.get("decision")]
        if not decided:
            return
        self.pdf.heading("Decisions already recorded" if not self.simple else "Things already decided", level=2)
        for finding in decided[:60]:
            decision = finding["decision"]
            ref = self.by_id.get(finding.get("reference_id"))
            line = ((_row(ref) + " — ") if ref else "") + f"{self._label(finding)[0]}: " \
                f"{DISPOSITION_WORDS.get(decision.get('disposition'), decision.get('disposition'))}"
            if decision.get("rationale"):
                line += f" — “{clip(decision['rationale'], 160)}”"
            if decision.get("reviewer_id"):
                line += f" ({decision['reviewer_id']})"
            self.pdf.bullet(line, size=9.5)

    def glossary(self) -> None:
        self.pdf.heading("Words you might meet" if not self.simple else "What the words mean", level=2)
        for key in GLOSSARY_ORDER:
            term, standard, simple = GLOSSARY[key]
            self.pdf.text(term, bold=True, size=10)
            self.pdf.text(simple if self.simple else standard, size=10, color=GRAPHITE, gap=3, indent=8)

    def limits(self) -> None:
        section = self.assistant._reliability(Opts(simple=self.simple))
        self.pdf.heading("What this report can and cannot tell you" if not self.simple else "Can I trust this?", level=2)
        self.pdf.text(section["text"], gap=2)
        for point in section["points"]:
            self.pdf.bullet(point)
        if not self.simple:
            shown = set(section["points"])
            for limitation in self.review.get("limitations") or []:
                if limitation not in shown:
                    self.pdf.bullet(limitation, color=GRAPHITE, size=9.5)

    def build(self) -> bytes:
        self.cover()
        self.document_level()
        self.how()
        self.found()
        self.todo()
        self.missing()
        self.self_citation()
        self.technical()
        self.notes()
        self.decisions()
        self.glossary()
        self.limits()
        return self.pdf.render()


def report_pdf(review: dict, *, artifact: Optional[dict] = None, style: str = "standard",
               manuscript: Optional[dict] = None, audit: Optional[dict] = None,
               generated_at: Optional[str] = None) -> bytes:
    """The plain-language report as PDF bytes."""
    if style not in ("standard", "simple"):
        raise ValueError(f"unsupported report style: {style}")
    return ReportBuilder(review, artifact=artifact, style=style, manuscript=manuscript, audit=audit,
                         generated_at=generated_at).build()


__all__ = ["REPORT_VERSION", "ReportBuilder", "report_pdf"]
