"""Signed review packages in PDF, HTML and JSON.

An export is the artifact that leaves the system, so it carries everything a
recipient needs to assess it without access to CitePrism: the review projection,
the human decisions, the correction proposals, the activity hash-chain head, the
scope limitations, and a manifest signed over all of it.

The PDF is generated with a small, dependency-light writer rather than a heavy
reporting stack, because a review package must still build on a locked-down
runner where installing a rendering toolchain is not an option.
"""

from __future__ import annotations

import html
import io
import json
import logging
import zipfile
from datetime import datetime, timezone

from src.evidence.review import assessment_diagnostics, build_review, report_html, status_label
from src.workspace.common import utc_now
from src.workspace.objects import content_key, digest_bytes
from src.workspace.signing import Signer

logger = logging.getLogger(__name__)

EXPORT_VERSION = "review-package-v1"
FORMATS = ("json", "html", "pdf", "bundle")
VARIANTS = ("review", "report")


def download_filename(record: dict) -> str:
    """The name a downloaded export is saved under."""
    suffix = {"json": "json", "html": "html", "pdf": "pdf", "bundle": "zip"}[record["format"]]
    kind = "report" if (record.get("manifest") or {}).get("variant") == "report" else "review"
    return f"citeprism-{kind}-{str(record['audit_id'])[:8]}.{suffix}"

LIMITATIONS = (
    "Automated proposals in this package are unreviewed model output. They are "
    "not findings of error, bias or research misconduct.",
    "A located quotation proves the text appears in the stored source snapshot. "
    "It does not establish that the citing interpretation is correct.",
    "Reference resolution is rule-based identity evidence. Agreement between "
    "scholarly indexes is recorded as evidence, not as a calibrated probability.",
    "This package reflects the audit at the moment of export. Later decisions, "
    "corrections or repairs are not included.",
)


# ---------------------------------------------------------------------
# Minimal PDF writer
# ---------------------------------------------------------------------
# Glyph advance widths (1/1000 em) of the base-14 Helvetica fonts for Windows-1252
# codes 32-255, from the Adobe AFM metrics. Accurate widths keep wrapped lines
# inside the margin without leaving ragged gaps.
_HELVETICA = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584, 761, 556, 0, 222, 556, 333, 1000, 556, 556, 333, 1000, 667, 333, 1000, 0, 611, 0, 0, 222, 222, 333, 333, 350, 556, 1000, 333, 1000, 500, 333, 944, 0, 500, 667, 278, 333, 556, 556, 556, 556, 260, 556, 333, 737, 370, 556, 584, 333, 737, 333, 400, 584, 333, 333, 333, 556, 537, 278, 333, 333, 365, 556, 834, 834, 834, 611, 667, 667, 667, 667, 667, 667, 1000, 722, 667, 667, 667, 667, 278, 278, 278, 278, 722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722, 667, 667, 611, 556, 556, 556, 556, 556, 556, 889, 500, 556, 556, 556, 556, 278, 278, 278, 278, 556, 556, 556, 556, 556, 556, 556, 584, 611, 556, 556, 556, 556, 500, 556, 500]
_HELVETICA_BOLD = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556, 333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584, 761, 556, 0, 278, 556, 500, 1000, 556, 556, 333, 1000, 667, 333, 1000, 0, 611, 0, 0, 278, 278, 500, 500, 350, 556, 1000, 333, 1000, 556, 333, 944, 0, 500, 667, 278, 333, 556, 556, 556, 556, 280, 556, 333, 737, 370, 556, 584, 333, 737, 333, 400, 584, 333, 333, 333, 611, 556, 278, 333, 333, 365, 556, 834, 834, 834, 611, 722, 722, 722, 722, 722, 722, 1000, 722, 667, 667, 667, 667, 278, 278, 278, 278, 722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722, 667, 667, 611, 556, 556, 556, 556, 556, 556, 889, 556, 556, 556, 556, 556, 278, 278, 278, 278, 611, 611, 611, 611, 611, 611, 611, 584, 611, 611, 611, 611, 611, 556, 611, 556]

INK = (0.09, 0.15, 0.22)
GRAPHITE = (0.36, 0.40, 0.46)
ACCENT = (0.09, 0.42, 0.45)
WARNING = (0.62, 0.20, 0.13)
CAUTION = (0.55, 0.38, 0.05)


class SimplePdf:
    """A deliberately small PDF/1.4 writer for text reports.

    Uses only the base-14 Helvetica fonts, so there is no font embedding, no
    external binary and no network access. It handles pagination, headings,
    wrapped body text with exact glyph widths, hanging indents, callout boxes,
    checkboxes and page numbers — the whole shape of a report.
    """

    PAGE_WIDTH, PAGE_HEIGHT = 595.28, 841.89  # A4 points
    MARGIN = 56.0
    FOOTER = 30.0

    def __init__(self, title: str, *, footer: str | None = None):
        self.title = title
        self.footer = footer
        self.pages: list[list[str]] = []
        self._current: list[str] = []
        self._y = self.PAGE_HEIGHT - self.MARGIN
        self._new_page()

    def _new_page(self) -> None:
        if self._current:
            self.pages.append(self._current)
        self._current = []
        self._y = self.PAGE_HEIGHT - self.MARGIN

    @property
    def _bottom(self) -> float:
        return self.MARGIN + (self.FOOTER if self.footer else 0)

    TRANSLITERATE = str.maketrans({"≥": ">=", "≤": "<=", "→": "›", "←": "<-", "≈": "~", "×": "x", "−": "-",
                                   "\u2009": " ", "\u202f": " ", "\u00a0": " ", "ł": "l", "Ł": "L", "ı": "i",
                                   "✓": "", "✔": "", "✗": "x", "☐": "[ ]"})

    @staticmethod
    def _escape(text: str) -> str:
        return (
            str(text)
            .replace("\\", r"\\")
            .replace("(", r"\(")
            .replace(")", r"\)")
        )

    @staticmethod
    def _latin1(text: str) -> str:
        """Text the base-14 fonts can show.

        The fonts are declared with WinAnsiEncoding, which is Windows-1252, not
        Latin-1: it has curly quotes, dashes, bullets and the ellipsis. Encoding
        as Latin-1 turned all of those into "?". Characters outside Windows-1252
        are transliterated where a plain equivalent exists.
        """
        import unicodedata
        value = str(text).translate(SimplePdf.TRANSLITERATE)
        result = []
        for character in value:
            if character in "\r\n\t":
                result.append(" ")
                continue
            try:
                character.encode("cp1252")
                result.append(character)
            except UnicodeEncodeError:
                plain = unicodedata.normalize("NFKD", character).encode("cp1252", "ignore").decode("cp1252")
                result.append(plain or "?")
        return "".join(result)

    @staticmethod
    def width(text: str, size: float, bold: bool = False) -> float:
        """Rendered width in points of already-encodable text."""
        table = _HELVETICA_BOLD if bold else _HELVETICA
        total = 0
        for code in str(text).encode("cp1252", "replace"):
            total += table[code - 32] if 32 <= code <= 255 else 556
        return total * size / 1000.0

    @staticmethod
    def _fit(word: str, size: float, bold: bool, width: float) -> int:
        """The longest prefix of `word` (at least one character) no wider than `width`."""
        from bisect import bisect_right
        from itertools import accumulate

        table = _HELVETICA_BOLD if bold else _HELVETICA
        advances = [(table[code - 32] if 32 <= code <= 255 else 556) * size / 1000.0
                    for code in word.encode("cp1252", "replace")]
        return max(1, bisect_right(list(accumulate(advances)), width))

    def _wrap(self, text: str, size: float, width: float, bold: bool = False) -> list[str]:
        words, lines, line = str(text).split(), [], ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if self.width(candidate, size, bold) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            # A single word wider than the line (a long URL or DOI) is broken where it
            # reaches the margin, found by bisection over cumulative glyph widths
            # (one pass per word, never one width computation per character).
            while self.width(word, size, bold) > width:
                cut = self._fit(word, size, bold, width - self.width("-", size, bold))
                lines.append(word[:cut] + "-")
                word = word[cut:]
            line = word
        if line:
            lines.append(line)
        return lines or [""]

    def _ensure(self, height: float) -> None:
        if self._y - height < self._bottom:
            self._new_page()

    @staticmethod
    def _rgb(color) -> str:
        r, g, b = color
        return f"{r:.3f} {g:.3f} {b:.3f} rg"

    def _line(self, x: float, line: str, size: float, bold: bool, color) -> None:
        font = "F2" if bold else "F1"
        self._current.append(
            f"BT {self._rgb(color)} /{font} {size} Tf {x:.2f} {self._y:.2f} Td "
            f"({self._escape(line)}) Tj ET"
        )

    def text(self, content: str, *, size: float = 10.5, bold: bool = False,
             leading: float | None = None, gap: float = 0.0, color=INK, indent: float = 0.0,
             first_prefix: str = "", prefix_width: float = 0.0) -> None:
        """Wrapped text. `first_prefix` hangs left of the first line (bullets, numbers)."""
        leading = leading or size * 1.38
        left = self.MARGIN + indent + prefix_width
        width = self.PAGE_WIDTH - self.MARGIN - left
        for index, line in enumerate(self._wrap(self._latin1(content), size, width, bold)):
            self._ensure(leading)
            if index == 0 and first_prefix:
                self._line(self.MARGIN + indent, self._latin1(first_prefix), size, bold, color)
            self._line(left, line, size, bold, color)
            self._y -= leading
        self._y -= gap

    def heading(self, content: str, *, level: int = 1, color=INK) -> None:
        size = {1: 18.0, 2: 13.5, 3: 11.5}.get(level, 11.0)
        # Keep a heading with at least a few lines of what follows it.
        if self._y < self._bottom + 90:
            self._new_page()
        else:
            self._y -= 8 if level <= 2 else 4
        self.text(content, size=size, bold=True, gap=4 if level <= 2 else 2, color=color)

    def rule(self, color=(0.55, 0.6, 0.65)) -> None:
        self._ensure(12)
        self._y -= 6
        r, g, b = color
        self._current.append(
            f"0.75 w {r:.3f} {g:.3f} {b:.3f} RG {self.MARGIN} {self._y:.2f} m "
            f"{self.PAGE_WIDTH - self.MARGIN} {self._y:.2f} l S"
        )
        self._y -= 10

    def field(self, label: str, value: str) -> None:
        self.text(f"{label}: {value}", size=10.0, leading=13.5)

    def bullet(self, content: str, *, indent: float = 0.0, color=INK, size: float = 10.0) -> None:
        self.text(content, size=size, leading=size * 1.38, indent=indent, color=color,
                  first_prefix="\u2022", prefix_width=12.0)

    def numbered(self, number: int, content: str, *, indent: float = 0.0, size: float = 10.0) -> None:
        self.text(content, size=size, leading=size * 1.38, indent=indent,
                  first_prefix=f"{number}.", prefix_width=16.0)

    def checkbox(self, content: str, *, detail: str | None = None, indent: float = 0.0,
                 color=INK) -> None:
        """A tick box with a bold first line and an optional plain detail line."""
        size = 10.0
        leading = size * 1.38
        left = self.MARGIN + indent + 18.0
        lines = self._wrap(self._latin1(content), size, self.PAGE_WIDTH - self.MARGIN - left, True)
        detail_lines = self._wrap(self._latin1(detail), 9.5, self.PAGE_WIDTH - self.MARGIN - left) if detail else []
        self._ensure(leading * min(len(lines) + len(detail_lines), 4) + 4)
        box_y = self._y - 1.5
        self._current.append(
            f"0.8 w {INK[0]:.3f} {INK[1]:.3f} {INK[2]:.3f} RG "
            f"{self.MARGIN + indent:.2f} {box_y:.2f} 9 9 re S"
        )
        for line in lines:
            self._ensure(leading)
            self._line(left, line, size, True, color)
            self._y -= leading
        for line in detail_lines:
            self._ensure(9.5 * 1.38)
            self._line(left, line, 9.5, False, GRAPHITE)
            self._y -= 9.5 * 1.38
        self._y -= 4

    def callout(self, content: str, *, title: str | None = None, tone=ACCENT) -> None:
        """A shaded box for the sentence a reader must not miss."""
        size, leading, pad = 10.5, 14.5, 10.0
        inner = self.PAGE_WIDTH - 2 * self.MARGIN - 2 * pad - 4
        lines = [(line, True) for line in self._wrap(self._latin1(title), size, inner, True)] if title else []
        lines += [(line, False) for line in self._wrap(self._latin1(content), size, inner)]
        height = len(lines) * leading + 2 * pad
        if height > self.PAGE_HEIGHT - self.MARGIN - self._bottom:
            # Too long for one box: fall back to plain text.
            if title:
                self.text(title, bold=True)
            self.text(content)
            return
        self._ensure(height + 6)
        top = self._y + leading * 0.3
        r, g, b = tone
        tint = tuple(1 - (1 - channel) * 0.10 for channel in tone)
        self._current.append(
            f"{tint[0]:.3f} {tint[1]:.3f} {tint[2]:.3f} rg {self.MARGIN:.2f} {top - height:.2f} "
            f"{self.PAGE_WIDTH - 2 * self.MARGIN:.2f} {height:.2f} re f"
        )
        self._current.append(
            f"{r:.3f} {g:.3f} {b:.3f} rg {self.MARGIN:.2f} {top - height:.2f} 3 {height:.2f} re f"
        )
        self._y = top - pad - leading * 0.75
        for line, bold in lines:
            self._line(self.MARGIN + pad + 4, line, size, bold, INK)
            self._y -= leading
        self._y = top - height - 10

    def spacer(self, amount: float = 8.0) -> None:
        self._y -= amount

    def render(self) -> bytes:
        self.pages.append(self._current)
        pages = [page for page in self.pages if page] or [[]]
        if self.footer:
            total = len(pages)
            for number, page in enumerate(pages, 1):
                label = self._escape(self._latin1(f"{self.footer}  \u00b7  page {number} of {total}"))
                page.append(f"BT {self._rgb(GRAPHITE)} /F1 8 Tf {self.MARGIN:.2f} {self.MARGIN - 20:.2f} Td "
                            f"({label}) Tj ET")
        objects: list[bytes] = []

        def add(payload: bytes) -> int:
            objects.append(payload)
            return len(objects)

        font_regular = add(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            b"/Encoding /WinAnsiEncoding >>"
        )
        font_bold = add(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
            b"/Encoding /WinAnsiEncoding >>"
        )
        pages_id = len(objects) + 1 + 2 * len(pages) + 1
        page_ids, content_ids = [], []
        for page in pages:
            stream = "\n".join(page).encode("cp1252", "replace")
            content_ids.append(
                add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                    + stream + b"\nendstream")
            )
        for content_id in content_ids:
            page_ids.append(
                add(
                    f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox "
                    f"[0 0 {self.PAGE_WIDTH:.2f} {self.PAGE_HEIGHT:.2f}] "
                    f"/Resources << /Font << /F1 {font_regular} 0 R "
                    f"/F2 {font_bold} 0 R >> >> /Contents {content_id} 0 R >>".encode()
                )
            )
        kids = " ".join(f"{identifier} 0 R" for identifier in page_ids)
        actual_pages_id = add(
            f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
        )
        # Document information strings are PDFDocEncoding or UTF-16BE, not Windows-1252:
        # an em dash written as cp1252 showed as "Š" in viewers' title bars.
        title = "".join(character for character in str(self.title) if character.isprintable())
        info_id = add(
            b"<< /Title <FEFF" + title.encode("utf-16-be").hex().upper().encode() + b"> /Producer (CitePrism) >>"
        )
        catalog_id = add(f"<< /Type /Catalog /Pages {actual_pages_id} 0 R >>".encode())

        buffer = io.BytesIO()
        buffer.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for number, payload in enumerate(objects, 1):
            offsets.append(buffer.tell())
            buffer.write(f"{number} 0 obj\n".encode() + payload + b"\nendobj\n")
        start = buffer.tell()
        buffer.write(f"xref\n0 {len(objects) + 1}\n".encode())
        buffer.write(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            buffer.write(f"{offset:010d} 00000 n \n".encode())
        buffer.write(
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R "
            f"/Info {info_id} 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
        )
        return buffer.getvalue()


# ---------------------------------------------------------------------
# Package content
# ---------------------------------------------------------------------
def review_package(review: dict, *, audit: dict, manuscript: dict, activity: dict,
                   exported_by: str) -> dict:
    """The JSON review package: everything a recipient needs, nothing more."""
    return {
        "package_version": EXPORT_VERSION,
        "generated_at": utc_now(),
        "exported_by": exported_by,
        "manuscript": {
            "id": manuscript.get("id"),
            "title": manuscript.get("title"),
            "original_filename": manuscript.get("original_filename"),
            "content_sha256": manuscript.get("content_sha256"),
            "text_layer_status": manuscript.get("text_layer_status", "unknown"),
        },
        "audit": {
            "id": audit.get("id"),
            "state": audit.get("state"),
            "created_at": audit.get("created_at"),
            "completed_at": audit.get("completed_at"),
            "summary": audit.get("summary", {}),
        },
        "review": review,
        "activity_verification": activity,
        "limitations": list(LIMITATIONS) + list(review.get("limitations", [])),
    }


def _short(value, limit: int) -> str:
    """Untrusted text from a PDF, cut to a length a report can show."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def package_pdf(package: dict, artifact: dict) -> bytes:
    """Render the review package as a paginated PDF."""
    review = package["review"]
    counts = review.get("counts", {})
    pdf = SimplePdf(f"CitePrism review — {_short(review.get('title') or 'Untitled', 200)}")

    pdf.heading("CitePrism evidence review", level=1)
    pdf.text(_short(review.get("title") or "Untitled manuscript", 400), size=12.5, bold=True, gap=2)
    pdf.rule()
    pdf.field("Audit", str(package["audit"]["id"]))
    pdf.field("Processing status", status_label(review.get("processing_status", "unknown")))
    pdf.field("Human review status", status_label(review.get("review_status", "not_reviewed")))
    pdf.field("Completed", str(package["audit"].get("completed_at") or "Not recorded"))
    pdf.field("Manuscript SHA-256", str(package["manuscript"].get("content_sha256") or "unknown"))
    pdf.field("Exported", f"{package['generated_at']} by {package['exported_by']}")
    verification = package.get("activity_verification") or {}
    pdf.field(
        "Activity chain",
        f"{'intact' if verification.get('valid') else 'FAILED VERIFICATION'} "
        f"({verification.get('event_count', 0)} events)",
    )
    pdf.spacer()

    references = {item["id"]: item for item in review.get("references", [])}
    findings = review.get("findings", [])
    priority_rank = {"high": 0, "moderate": 1, "low": 2, "informational": 3}
    priority_word = {"high": "Serious", "moderate": "Worth a look", "low": "Minor", "informational": "For information"}

    # 1. What a reader must know first: whole-document findings (reliability,
    #    several reference lists, self-citation pattern, missing literature).
    document_level = sorted((f for f in findings if not f.get("reference_id")),
                            key=lambda f: priority_rank.get(f["priority"], 9))
    pdf.heading("At a glance", level=2)
    attention = [f for f in findings if f.get("reference_id") and f["priority"] != "informational"
                 and not f.get("system_issue")]
    attention_refs = {f["reference_id"] for f in attention}
    pdf.text(
        f"{counts.get('total_references', 0)} references were checked. "
        f"{len(attention_refs)} have something worth a look"
        + (f"; {sum(f['priority'] == 'high' for f in attention)} item(s) are serious." if attention else "."),
        size=10.5, leading=14.0)
    for finding in document_level:
        pdf.text(f"{priority_word.get(finding['priority'], finding['priority'])}: {finding['summary']}",
                 size=10.5, bold=True, leading=14.0)
        pdf.text(finding["explanation"], size=9.5, leading=12.5)
        pdf.text(f"Next step: {finding['action']}", size=9.5, leading=12.5)
        pdf.spacer(4)
    pdf.spacer()

    pdf.heading("Summary", level=2)
    year_conflicts = sum(f["category"] == "citation_year_mismatch" for f in findings)
    for label, value in (
        ("References", counts.get("total_references", 0)),
        ("Citation occurrences located", counts.get("citation_occurrences", 0)),
        ("Matched to a published record", counts.get("resolved_references", 0)),
        ("Not matched", counts.get("unresolved_references", 0)),
        ("Relevance scored", counts.get("relevance_scored", 0)),
        ("Possibly irrelevant", counts.get("potentially_irrelevant", 0)),
        ("Self-citations", counts.get("self_citations", 0)),
        ("Details differing from the published record", counts.get("metadata_inconsistencies", 0)),
        ("Cited with a different year than listed", year_conflicts),
        ("Possibly missing works", counts.get("possibly_missing_works", 0)),
        ("Technical issues", counts.get("system_issues", 0)),
        ("Recorded reviewer decisions", counts.get("reviewer_decisions", 0)),
    ):
        pdf.field(label, str(value))
    pdf.spacer()

    # 2. One block per reference, most serious first; notes for information
    #    are summarised separately instead of repeated under every reference.
    pdf.heading("References to check", level=2)
    by_reference: dict[str, list[dict]] = {}
    for finding in findings:
        if finding.get("reference_id") and finding["priority"] != "informational":
            by_reference.setdefault(finding["reference_id"], []).append(finding)
    ordered = sorted(by_reference.items(), key=lambda item: (
        min(priority_rank.get(f["priority"], 9) for f in item[1]),
        str(references.get(item[0], {}).get("marker", ""))))
    if not ordered:
        pdf.text("No reference needs a closer look.")
    for reference_id, items in ordered:
        reference = references.get(reference_id, {})
        worst = min(items, key=lambda f: priority_rank.get(f["priority"], 9))["priority"]
        pdf.text(f"{_short(reference.get('marker', ''), 60)} {_short(reference.get('title', ''), 400)}", size=10.5,
                 bold=True, leading=13.5)
        pdf.text(priority_word.get(worst, worst), size=9.0, leading=11.5)
        for finding in sorted(items, key=lambda f: priority_rank.get(f["priority"], 9)):
            pdf.bullet(f"{finding['summary']}. {finding['explanation']} Next step: {finding['action']}")
            decision = finding.get("decision")
            if decision:
                pdf.text(f"Decision: {status_label(decision['disposition'])} by {decision['reviewer_id']} — "
                         f"{decision['rationale']}", size=9.0, leading=12.0)
            if finding.get("system_issue"):
                pdf.text("A CitePrism processing issue, not a problem with the manuscript.", size=9.0, leading=12.0)
        pdf.spacer(5)

    notes: dict[str, list[str]] = {}
    summaries: dict[str, str] = {}
    for finding in findings:
        if finding.get("reference_id") and finding["priority"] == "informational":
            notes.setdefault(finding["category"], []).append(str(references.get(finding["reference_id"], {}).get("marker", "")))
            summaries[finding["category"]] = finding["summary"]
    if notes:
        pdf.heading("Notes for information", level=2)
        for category, markers in sorted(notes.items(), key=lambda item: -len(item[1])):
            shown = ", ".join(markers[:40]) + (f" and {len(markers) - 40} more" if len(markers) > 40 else "")
            pdf.bullet(f"{summaries[category]} — {len(markers)} reference(s): {shown}")
        pdf.spacer()

    corrections = review.get("corrections") or []
    if corrections:
        pdf.heading("Correction proposals", level=2)
        for correction in corrections:
            pdf.text(
                f"{correction['kind']} — {status_label(correction['state'])}",
                size=10.0,
                bold=True,
                leading=13.0,
            )
            pdf.text(f"Rationale: {correction['rationale']}", size=9.5, leading=12.5)
            pdf.text(
                f"Proposed: {json.dumps(correction.get('proposed_value', {}), ensure_ascii=False)}",
                size=9.5,
                leading=12.5,
            )
            pdf.spacer(4)

    diagnostics = assessment_diagnostics(artifact)
    if diagnostics:
        pdf.heading("Assessment checks", level=2)
        pdf.text(
            "A successful provider call is not an accepted assessment. Each "
            "response must also pass its claim and quotation checks.",
            size=9.5,
        )
        for item in diagnostics[:120]:
            pdf.bullet(
                f"{item['reference']}: {status_label(item['status'])} — {item['detail']}"
            )
        pdf.spacer()

    pdf.heading("Scope and limitations", level=2)
    for limitation in package["limitations"]:
        pdf.bullet(limitation)

    return pdf.render()


def package_html(package: dict, artifact: dict) -> bytes:
    """HTML export, reusing the Phase 2 report and adding the human layer."""
    base = report_html(artifact)
    review = package["review"]
    escape = lambda value: html.escape(str(value), quote=True)  # noqa: E731

    decisions = "".join(
        f"<tr><td>{escape(finding['summary'])}</td>"
        f"<td>{escape(status_label(finding['decision']['disposition']))}</td>"
        f"<td>{escape(finding['decision']['reviewer_id'])}</td>"
        f"<td>{escape(finding['decision']['rationale'])}</td></tr>"
        for finding in review.get("findings", [])
        if finding.get("decision")
    )
    verification = package.get("activity_verification") or {}
    header = (
        "<section><h2>Review package</h2>"
        f"<p>Audit {escape(package['audit']['id'])} · exported "
        f"{escape(package['generated_at'])} by {escape(package['exported_by'])}</p>"
        f"<p>Manuscript SHA-256: <code>{escape(package['manuscript']['content_sha256'])}</code></p>"
        f"<p>Activity chain: {'intact' if verification.get('valid') else 'FAILED VERIFICATION'} "
        f"({escape(verification.get('event_count', 0))} events)</p>"
        + (
            "<h3>Recorded reviewer decisions</h3><table><thead><tr><th>Item</th>"
            "<th>Disposition</th><th>Reviewer</th><th>Rationale</th></tr></thead>"
            f"<tbody>{decisions}</tbody></table>"
            if decisions
            else "<p>No reviewer decisions were recorded before this export.</p>"
        )
        + "</section>"
    )
    marker = "<h2>Scope and limitations</h2>"
    body = base.replace(marker, header + marker) if marker in base else base + header
    return body.encode("utf-8")


def package_json(package: dict) -> bytes:
    return json.dumps(package, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")


def build_manifest(package: dict, files: list[dict], *, tenant_id: str) -> dict:
    return {
        "manifest_version": EXPORT_VERSION,
        "tenant_id": tenant_id,
        "audit_id": package["audit"]["id"],
        "manuscript_sha256": package["manuscript"]["content_sha256"],
        "generated_at": package["generated_at"],
        "exported_by": package["exported_by"],
        "review_status": package["review"].get("review_status"),
        "processing_status": package["review"].get("processing_status"),
        "counts": package["review"].get("counts", {}),
        "activity_head_hash": (package.get("activity_verification") or {}).get("head_hash"),
        "activity_chain_valid": (package.get("activity_verification") or {}).get("valid"),
        "files": files,
        "limitations": package["limitations"],
    }


class ExportService:
    """Build, sign and store review packages."""

    def __init__(self, service, store, object_store, *, signer: Signer | None = None,
                 retention_days: int = 365, tenant_namespace: str | None = None):
        self.service = service
        self.store = store
        self.object_store = object_store
        self.signer = signer or Signer("ed25519")
        self.retention_days = retention_days
        self.tenant_namespace = tenant_namespace

    def create(self, tenant_id: str, audit_id: str, export_format: str, *, actor_id: str,
               variant: str = "review", style: str = "standard") -> dict:
        """Build, sign and store one export.

        `variant` "review" is the formal review package; "report" is the
        plain-language report (PDF only) with a checklist of recommended steps,
        in the `style` "standard" or "simple". Both are signed the same way.
        """
        if export_format not in FORMATS:
            raise ValueError(f"unsupported export format: {export_format}")
        if variant not in VARIANTS:
            raise ValueError(f"unsupported export variant: {variant}")
        if variant == "report" and export_format != "pdf":
            raise ValueError("the plain-language report is available as PDF only")
        if style not in ("standard", "simple"):
            raise ValueError(f"unsupported report style: {style}")

        artifact = self.service.load_artifact(tenant_id, audit_id)
        review = self.service.review(tenant_id, audit_id)
        audit = self.service.get_audit(tenant_id, audit_id)
        audit_row = audit.model_dump(mode="json") if hasattr(audit, "model_dump") else dict(audit)
        manuscript = self.service.get_manuscript(tenant_id, audit_row["manuscript_id"])
        manuscript_row = (
            manuscript.model_dump(mode="json")
            if hasattr(manuscript, "model_dump")
            else dict(manuscript)
        )
        activity = self.store.verify_activity_chain(tenant_id)

        package = review_package(
            review,
            audit=audit_row,
            manuscript=manuscript_row,
            activity=activity,
            exported_by=actor_id,
        )

        def plain_report(report_style: str) -> bytes:
            from src.assistant.report import report_pdf  # imported here: the report builds on this module
            return report_pdf(review, artifact=artifact, style=report_style, manuscript=manuscript_row,
                              audit=audit_row, generated_at=package["generated_at"])

        # File name -> (bytes, media type). Every file is listed in the signed manifest.
        renditions: dict[str, tuple[bytes, str]] = {}
        if variant == "report":
            renditions["report.pdf"] = (plain_report(style), "application/pdf")
        else:
            renditions["review.json"] = (package_json(package), "application/json")
            if export_format in {"html", "pdf", "bundle"}:
                renditions["review.html"] = (package_html(package, artifact), "text/html; charset=utf-8")
            if export_format in {"pdf", "bundle"}:
                renditions["review.pdf"] = (package_pdf(package, artifact), "application/pdf")
            if export_format == "bundle":
                # The bundle also carries the plain-language report, so a recipient
                # who is not a reviewer has something to act on.
                renditions["report.pdf"] = (plain_report("standard"), "application/pdf")

        files = [
            {
                "name": name,
                "media_type": media_type,
                "size_bytes": len(payload),
                "sha256": digest_bytes(payload),
            }
            for name, (payload, media_type) in sorted(renditions.items())
        ]
        manifest = build_manifest(package, files, tenant_id=tenant_id)
        manifest["variant"] = variant
        if variant == "report":
            manifest["report_style"] = style
        signature = self.signer.sign_manifest(manifest)
        manifest_with_signature = {**manifest, "signature": signature.as_dict()}

        if export_format == "bundle":
            payload, media_type = self._zip(renditions, manifest_with_signature), "application/zip"
        elif variant == "report":
            payload, media_type = renditions["report.pdf"]
        else:
            payload, media_type = renditions[f"review.{export_format}"]

        digest = digest_bytes(payload)
        namespace = self.tenant_namespace or tenant_id
        suffix = {
            "json": ".json",
            "html": ".html",
            "pdf": ".pdf",
            "bundle": ".zip",
        }[export_format]
        key = content_key(namespace, "exports", digest, suffix=suffix)
        reference = self.object_store.put_bytes(payload, key=key, media_type=media_type)

        record = self.store.record_export(
            tenant_id,
            audit_id=audit_id,
            format=export_format,
            object_backend=reference.backend,
            object_key=reference.key,
            object_version_id=reference.version_id,
            content_sha256=reference.content_sha256,
            size_bytes=reference.size_bytes,
            signature_algorithm=signature.algorithm,
            signature_key_id=signature.key_id,
            signature=signature.value,
            manifest=manifest_with_signature,
            created_by=actor_id,
            retention_days=self.retention_days,
        )
        record["media_type"] = media_type
        record["download_filename"] = download_filename(record)
        return record

    @staticmethod
    def _zip(renditions: dict[str, tuple[bytes, str]], manifest: dict) -> bytes:
        buffer = io.BytesIO()
        # A fixed timestamp keeps the bundle reproducible: the same review always
        # produces the same bytes, so a recipient can compare two exports.
        fixed = (2026, 1, 1, 0, 0, 0)
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, (payload, _media_type) in sorted(renditions.items()):
                info = zipfile.ZipInfo(name, date_time=fixed)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, payload)
            info = zipfile.ZipInfo("manifest.json", date_time=fixed)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(
                info,
                json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False),
            )
            readme = zipfile.ZipInfo("README.txt", date_time=fixed)
            readme.external_attr = 0o644 << 16
            archive.writestr(readme, BUNDLE_README)
        return buffer.getvalue()

    def download(self, tenant_id: str, export_id: str) -> tuple[dict, bytes]:
        record = self.store.get_export(tenant_id, export_id)
        payload = self.object_store.get_bytes(
            record["object_key"], expected_sha256=record["content_sha256"]
        )
        return record, payload

    def signed_url(self, tenant_id: str, export_id: str) -> str | None:
        record = self.store.get_export(tenant_id, export_id)
        return self.object_store.presigned_url(record["object_key"])


BUNDLE_README = """CitePrism review package
========================

Contents
  review.json    The complete review projection, decisions and corrections.
  review.html    The same content as a portable, self-contained web page.
  review.pdf     A paginated copy for printing or filing.
  report.pdf     The plain-language report: what was found and a checklist of
                 recommended steps, for readers who are not reviewers.
  manifest.json  File digests plus a detached signature over this manifest.

How to verify this package
  1. Recompute the SHA-256 of each file and compare it with manifest.json.
  2. Verify manifest.signature over the manifest with every key EXCEPT the
     "signature" field, serialized as compact JSON with sorted keys.
  3. For an ed25519 signature, use the public key recorded in the manifest or
     the published deployment key. A key id ending in "-ephemeral" was generated
     at export time and cannot attribute this package to a deployment.

What this package does and does not claim
  Automated proposals are unreviewed model output, not findings of error, bias
  or research misconduct. A located quotation proves the text appears in the
  stored source snapshot; it does not establish that the citing interpretation
  is correct. Every item requires human judgement.
"""


__all__ = [
    "EXPORT_VERSION",
    "ExportService",
    "FORMATS",
    "SimplePdf",
    "build_manifest",
    "package_html",
    "package_json",
    "package_pdf",
    "review_package",
]
