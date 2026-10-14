"""Detect chapters in edited volumes and split them into separate manuscripts.

An edited book, a proceedings volume or a cumulative thesis has one reference
list per chapter. Each chapter has its own authors, so self-citation must be
judged per chapter, and each chapter's citations point only at its own list.
CitePrism therefore checks such documents chapter by chapter: every part runs
from the page after the previous reference list ends to the page where its own
reference list ends, and becomes a manuscript of its own. The original PDF is
kept unchanged.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from src.evidence import bibliography as bib
from src.evidence.extraction import front_matter, joined_source, pdf_source, read_pdf_isolated, span_locations

MIN_ENTRIES_PER_PART = 3


def _opening(page: dict) -> bool:
    """A chapter's first page: substantial text that starts with its authors' names.

    Continuation pages start with a page number and running head; part
    dividers are a few words; neither qualifies.
    """
    from src.evidence.extraction import NAME_LINE

    text = page["text"]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(text) < 400 or not lines or re.fullmatch(r"\d{1,4}", lines[0]):
        return False
    first = lines[0].rstrip(", ")
    return bool(NAME_LINE.match(first)) and not re.search(r"\d", first)


def detect_parts(pdf_path: str | Path, source: dict | None = None) -> dict:
    """The document's reference lists and the page range of the chapter each closes.

    A chapter starts at its opening page (authors' names, then the title) and
    ends just before the next chapter's opening page, so figure lists and
    appendices printed after a reference list stay with their chapter.
    """
    source = source or read_pdf_isolated(pdf_path)
    text, pages = joined_source(source)
    sections = [section for section in bib.find_sections(text) if len(section.entries) >= MIN_ENTRIES_PER_PART]
    by_number = {page["page"]: page for page in source["pages"]}
    spans, previous_list_end = [], 0
    for number, section in enumerate(sections, 1):
        list_pages = sorted({loc["page"] for loc in span_locations(pages, section.heading_start, section.end)})
        if not list_pages:
            continue
        heading_page = list_pages[0]
        openings = [n for n in range(previous_list_end + 1, heading_page + 1) if n in by_number and _opening(by_number[n])]
        start = openings[0] if openings else previous_list_end + 1
        spans.append({"number": number, "section": section, "start": start, "list_end": list_pages[-1]})
        previous_list_end = list_pages[-1]
    parts = []
    for index, span in enumerate(spans):
        end = spans[index + 1]["start"] - 1 if index + 1 < len(spans) else len(source["pages"])
        if index + 1 == len(spans):
            # After the last chapter: keep trailing pages only while they are not another opening.
            later = [n for n in range(span["list_end"] + 1, end + 1) if _opening(by_number[n])]
            end = later[0] - 1 if later else span["list_end"]
        part_pages = [by_number[n] for n in range(span["start"], end + 1) if n in by_number]
        # Only an opening page names a chapter's authors; front matter (cover,
        # contents, editorial) gets no guess rather than a wrong one.
        guess = (front_matter({**source, "pages": part_pages, "document_info": {}})
                 if span["start"] in by_number and _opening(by_number[span["start"]]) else {})
        parts.append({
            "number": span["number"],
            "page_start": span["start"],
            "page_end": end,
            "pages": end - span["start"] + 1,
            "title": guess.get("title"),
            "authors": guess.get("authors", []),
            "references": len(span["section"].entries),
            "reference_heading": span["section"].heading,
        })
    return {
        "multi_part": len(parts) > 1,
        "parts": parts,
        "pages": len(source["pages"]),
        "reference_lists": len(sections),
        "note": ("Each part has its own reference list. Checking parts separately keeps every chapter's "
                 "citations, authors and self-citation analysis apart.") if len(parts) > 1 else None,
    }


def part_pdf(pdf_path: str | Path, page_start: int, page_end: int) -> bytes:
    """A new PDF with pages page_start..page_end (1-based, inclusive)."""
    import pypdf

    reader = pypdf.PdfReader(str(pdf_path))
    writer = pypdf.PdfWriter()
    for index in range(page_start - 1, min(page_end, len(reader.pages))):
        writer.add_page(reader.pages[index])
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


SMALL_WORDS = {"a", "an", "and", "as", "at", "by", "for", "in", "of", "on", "or", "the", "to", "with",
               "im", "der", "die", "das", "des", "dem", "den", "und", "oder", "für", "zu", "zum", "zur",
               "von", "vom", "mit", "auf", "aus", "bei", "ein", "eine", "et", "la", "le", "les", "de", "du"}


def readable_case(text: str) -> str:
    """Soften a title printed in capitals; mixed-case titles are left as printed."""
    letters = [c for c in text if c.isalpha()]
    if not letters or sum(c.isupper() for c in letters) / len(letters) < .8:
        return text
    words = text.lower().split(" ")
    return " ".join(word if index and word in SMALL_WORDS else word[:1].upper() + word[1:]
                    for index, word in enumerate(words))


def short_book_title(book_title: str) -> str:
    """The book's main title, without its subtitle."""
    main = re.split(r"\s*:\s+|\s+[–—]\s+", (book_title or "").strip(), maxsplit=1)[0]
    return readable_case(re.sub(r"\s+", " ", main)) or "Book"


def part_title(book_title: str, part: dict) -> str:
    """Chapter first, so chapters of one book stay distinguishable in a list."""
    title = re.sub(r"\s+", " ", (part.get("title") or "").strip())
    if not title:
        title = (f"Opening pages (pp. {part['page_start']}–{part['page_end']})" if part["number"] == 1
                 else f"Pages {part['page_start']}–{part['page_end']}")
    if len(title) > 110:
        title = title[:107].rstrip() + "…"
    return f"{part['number']}. {readable_case(title)} — {short_book_title(book_title)}"


def part_filename(original_filename: str, part: dict) -> str:
    """A readable file name derived from the uploaded file, never the stored hash."""
    stem = Path(original_filename or "book.pdf").stem
    stem = re.sub(r"^\d{8,}_", "", stem)  # upload-tool timestamps
    stem = re.sub(r"[^\w\-]+", "_", stem).strip("_")[:60] or "book"
    return f"{stem}_ch{part['number']:02d}_p{part['page_start']}-{part['page_end']}.pdf"


__all__ = ["detect_parts", "part_filename", "part_pdf", "part_title", "readable_case"]
