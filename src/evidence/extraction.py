"""Preserve printed entries and locate citation occurrences in extracted page text."""

import bisect
import copy
import os
import re
from collections import Counter
from pathlib import Path

from src.evidence import EXTRACTION_VERSION
from src.evidence import bibliography as bib
from src.evidence.text import bibliographic_text, canonical_id, digest_json, digest_text, marker_ids
from src.evidence.textclasses import LOWER, NAME_CHAR, UPPER
from src.utils.reference_matching import normalize_doi

# Multilingual: "Literatur", "Literaturverzeichnis", "Références", "Bibliografía", …
REFERENCE_HEADER = bib.SECTION_HEADING
ENTRY_MARKER = re.compile(r"(?m)^[ \t]*(\[\d+\]|\(\d+\)|\d+[.)])[ \t]+")
# "[1]–[3]" (IEEE ranges) before single brackets, so the range is one citation of 1, 2 and 3.
NUMERIC_RANGE = r"\[\s*\d+\s*\]\s*[–—-]\s*\[\s*\d+\s*\]"
NUMERIC_CITATION = r"\[\s*\d+(?:\s*(?:[,;–—-])\s*\d+)*\s*\]"
# Names are atomic and "and" needs spaces on both sides, so text such as
# "XaandYaandYa…" cannot be split in exponentially many ways.
_CITED_NAME = rf"(?>{UPPER}{NAME_CHAR}+)"
AUTHOR_CITATION = (
    rf"\({_CITED_NAME}(?:\s+et\s+al\.)?"
    rf"(?:(?:\s*[,&]\s*|\s+and\s+){_CITED_NAME})*,\s*\d{{4}}[a-z]?"
    r"(?:,\s*(?:pp?\.)?\s*\d+(?:[-–]\d+)?)?\)"
    rf"|(?<![\w'’\-]){_CITED_NAME}(?:\s+(?:&|and)\s+{_CITED_NAME})?"
    r"(?:\s+et\s+al\.)?\s*\(\d{4}[a-z]?\)"
)
CITATION_PATTERN = re.compile(f"{NUMERIC_RANGE}|{NUMERIC_CITATION}|{AUTHOR_CITATION}")


MAX_PDF_PAGES = int(os.getenv("CITEPRISM_MAX_PDF_PAGES", "2000"))
MAX_PDF_CHARACTERS = int(os.getenv("CITEPRISM_MAX_PDF_CHARACTERS", "20000000"))
PDF_TIMEOUT_SECONDS = float(os.getenv("CITEPRISM_PDF_TIMEOUT_SECONDS", "180"))
PDF_MEMORY_MB = int(os.getenv("CITEPRISM_PDF_MEMORY_MB", "2048"))
SURROGATES = re.compile(r"[\ud800-\udfff]")
# Invisible characters that only break matching: zero-width spaces and joiners,
# the byte-order mark and bidirectional overrides ("Zw\u200bei" is "Zwei").
INVISIBLE = re.compile(r"[\u200b-\u200d\u2060\ufeff\u202a-\u202e\u2066-\u2069]")


class PdfLimitError(ValueError):
    """The PDF exceeds what CitePrism reads: too many pages, too much text, too slow."""


def pdf_source(path, method="pypdf"):
    import pypdf

    path = Path(path)
    pages = []
    reader = pypdf.PdfReader(path)
    if reader.is_encrypted:
        raise ValueError("Encrypted PDF: provide an unlocked copy for extraction.")
    if len(reader.pages) > MAX_PDF_PAGES:
        raise PdfLimitError(f"The PDF has {len(reader.pages)} pages; CitePrism reads at most {MAX_PDF_PAGES}. "
                            "Split it into parts first.")
    characters = 0
    pdfminer_pages = None
    if method == "pdfminer":
        # One pass over the document; pdfminer ends every page with a form feed. Reading it
        # page by page re-parsed the whole file for each page (quadratic on long books).
        from pdfminer.high_level import extract_text
        pdfminer_pages = extract_text(str(path)).split("\x0c")
    for index, page in enumerate(reader.pages, 1):
        if pdfminer_pages is not None:
            text = pdfminer_pages[index - 1] if index - 1 < len(pdfminer_pages) else ""
        else:
            text = page.extract_text() or ""
        # A broken font map can yield lone surrogates, which no JSON or hash accepts.
        text = INVISIBLE.sub("", SURROGATES.sub("\ufffd", text))
        characters += len(text)
        if characters > MAX_PDF_CHARACTERS:
            raise PdfLimitError("The PDF contains more text than CitePrism reads in one audit. Split it into parts.")
        pages.append({"page": index, "text": text, "text_sha256": digest_text(text)})
    info = {}
    try:
        metadata = reader.metadata or {}
        info = {key: str(metadata.get(f"/{name}")).strip() for key, name in (("title", "Title"), ("author", "Author"))
                if metadata.get(f"/{name}") and str(metadata.get(f"/{name}")).strip()}
    except Exception:  # noqa: BLE001 - document info is optional
        info = {}
    return {"pdf_sha256": __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
            "method": method, "coordinate_system": "extracted_page_text_characters",
            "pages": pages, "document_info": info}


def read_pdf_isolated(path, method="pypdf", *, timeout: float | None = None) -> dict:
    """`pdf_source` in a separate interpreter with a time limit (and a memory limit
    where the operating system supports one).

    An uploaded PDF is untrusted: a few hundred kilobytes can decompress into
    gigabytes, or take minutes to parse. Reading it in a child process means such
    a file costs one bounded attempt and a clear error, never a hung server.
    Set CITEPRISM_PDF_ISOLATION=off to read in-process (debugging only).
    """
    if os.getenv("CITEPRISM_PDF_ISOLATION", "on").strip().lower() in {"off", "0", "false", "no"}:
        return pdf_source(path, method)
    import json
    import subprocess
    import sys

    timeout = PDF_TIMEOUT_SECONDS if timeout is None else timeout
    root = str(Path(__file__).resolve().parents[2])
    environment = {**os.environ, "PYTHONPATH": root + os.pathsep + os.environ.get("PYTHONPATH", ""),
                   "PYTHONIOENCODING": "utf-8"}
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "src.evidence.pdf_worker", str(Path(path).resolve()), method, str(PDF_MEMORY_MB)],
            cwd=root, env=environment, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise PdfLimitError(f"Reading the PDF took longer than {round(timeout)} seconds. It may be damaged or "
                            "built to overload readers; try exporting it again as a standard PDF.") from None
    try:
        outcome = json.loads(completed.stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise PdfLimitError("Reading the PDF stopped unexpectedly (it may need more memory than allowed).") from None
    if outcome.get("ok"):
        return outcome["source"]
    kind, message = outcome.get("kind"), outcome.get("message") or "The PDF could not be read."
    raise (PdfLimitError if kind in {"PdfLimitError", "MemoryError"} else ValueError)(message)


_NAME_PART = rf"{UPPER}(?:{LOWER}|['’])+(?:-{UPPER}?(?:{LOWER}|['’])+)*"
NAME_LINE = re.compile(
    rf"^(?:{_NAME_PART}\.?(?:\s+{UPPER}\.)?(?:\s+(?:van|von|de|der|den|da|du)\b)?"
    rf"(?:\s+{_NAME_PART}){{1,2}}\s*(?:,|&|and|und)?\s*){{1,8}}$")


def front_matter(source):
    """Title and authors without a model: PDF document info, then the first text page.

    Used only when every configured model is unavailable. Author lines are
    recognised as lines made solely of personal names ("Benno Volk, Dana Blume").
    """
    info = source.get("document_info") or {}
    title = info.get("title")
    authors = []
    author_field = re.sub(r"\((?:hg|hrsg|eds?)\.?\)", "", info.get("author") or "", flags=re.I)
    placeholder = re.fullmatch(r"(?i)\s*(?:student|user|admin(?:istrator)?|owner|author|unknown|"
                               r"microsoft office user|windows user|[A-Z]{2,12})\s*", author_field or "")
    content_pages = [page for page in source["pages"] if len(page["text"]) >= 400]
    # Beyond the first content page, the PDF's own author field is the better witness.
    scan = content_pages[:1] if author_field and not placeholder else content_pages[:3]
    for page in scan:
        lines = [line.strip() for line in page["text"].splitlines() if line.strip()][:15]
        for index, line in enumerate(lines):
            candidate = line.rstrip(", ")
            if NAME_LINE.match(candidate) and not re.search(r"\d", candidate):
                block, last, cursor = [candidate], line, index
                for following in lines[index + 1:index + 4]:      # an author list may wrap
                    if last.rstrip().endswith(",") and NAME_LINE.match(following.rstrip(", ")):
                        block.append(following.rstrip(", "))
                        last, cursor = following, cursor + 1
                    else:
                        break
                authors = [name.strip() for part in block
                           for name in re.split(r",|&|\band\b|\bund\b", part) if name.strip()]
                if not title:
                    heading = []
                    for following in lines[cursor + 1:cursor + 7]:
                        if len(following) > 60 or following.endswith((".", ":")) and heading:
                            break
                        heading.append(following)
                    title = re.sub(r"\s+", " ", " ".join(heading)).strip() or None
                break
        if authors:
            break
    if not authors and author_field and not placeholder:
        authors = [name.strip() for name in re.split(r",|&|;|\band\b|\bund\b", author_field) if name.strip()]
    return {"title": title, "authors": authors[:20], "doi": None, "abstract": None}


def joined_source(source):
    offset, pages = 0, []
    for page in source["pages"]:
        pages.append({**page, "global_start": offset, "global_end": offset + len(page["text"])})
        offset += len(page["text"]) + 2
    return "\n\n".join(p["text"] for p in pages), pages


def span_locations(pages, start, end):
    locations = []
    for page in pages:
        left, right = max(start, page["global_start"]), min(end, page["global_end"])
        if right > left:
            locations.append({"page": page["page"], "char_start": left - page["global_start"],
                              "char_end": right - page["global_start"],
                              "page_text_sha256": page["text_sha256"],
                              "text": page["text"][left-page["global_start"]:right-page["global_start"]]})
    return locations


def reference_boundary(text):
    """The heading that opens the (last) reference list.

    A heading word can also title a body section ("2. Literature"), so the last
    heading actually followed by entries — numbered or author–year — wins.
    """
    matches = list(REFERENCE_HEADER.finditer(text))
    for match in reversed(matches):
        following = text[match.end():match.end() + 1500]
        if ENTRY_MARKER.search(following) or bib.split_entries(text, match.end(), min(len(text), match.end() + 4000)):
            return match
    return matches[-1] if matches else None


def _marker_style(token: str) -> str:
    return "bracket" if token.startswith("[") else "paren" if token.startswith("(") else \
        "dot" if token.endswith(".") else "close"


APPENDIX = re.compile(r"(?i)\b(?:appendix|appendices|anhang|anlage|annex|fragebogen|questionnaire|interview\w*|"
                      r"leitfaden|items?|survey|umfrage|tabelle|table|abbildung|figure|glossar\w*|index)\b")
BIBLIOGRAPHIC = re.compile(r"(?<!\d)(?:1[5-9]|20)\d\d[a-z]?(?!\d)|\bn\.\s?d\.|\bo\.\s?J\.|\bi\.\s?E\.|"
                           r"\bin press\b|\bdoi\b|https?://|www\.|\bisbn\b", re.IGNORECASE)


def _runs(items, kind=lambda match: None):
    """Runs of entry numbers counting up by one in document order, joined across small gaps.

    `items` are (match, number) in document order. A gap of up to three missing
    numbers (entries whose number did not start a line) joins two runs, in any order,
    so a list extracted as 1–10, 21–30, 11–20 is one run. Two stray line starts never
    join each other ("70. Jahrgang" … "73. Jahrgang"): a join needs a run of three, or
    two runs of two. Linear in the number of markers.
    """
    runs: list[list] = []
    seen: dict[int, list[int]] = {}
    for match, number in items:
        seen.setdefault(number, []).append(match.start())
    def moved_on(after: int, number: int, before: int) -> bool:
        """Whether the list went on past `number` between two positions: an "8" followed by a
        "9" there means a later "7" is a stray line start ("7. Aufl."), not the missing entry.
        (The earliest "8" in the stretch decides: if any "9" follows an "8" there, one follows it.)"""
        nexts, laters = seen.get(number + 1, ()), seen.get(number + 2, ())
        index = bisect.bisect_right(nexts, after)
        if index >= len(nexts) or nexts[index] >= before:
            return False
        later = bisect.bisect_right(laters, nexts[index])
        return later < len(laters) and laters[later] < before

    by_last: dict[int, list[list]] = {}
    for match, number in items:
        # Only a run of the same kind continues (a reference after references, not after the
        # points of an annotation or a "12. Jahrgang" line); runs of different kinds are joined
        # below only where nothing else claims the place.
        options = [run for run in by_last.get(number - 1, ())
                   if kind(run[-1][0]) == kind(match) and not moved_on(run[-1][0].start(), number, match.start())]
        if options:
            # The longest run continues, then the one read most recently (a second list
            # "1–5 … 1–12" goes on after its own 5, not the first list's).
            run = max(options, key=lambda run: (len(run), run[-1][0].start()))
            by_last[number - 1].remove(run)
            run.append((match, number))
        else:
            run = [(match, number)]
            runs.append(run)
        by_last.setdefault(number, []).append(run)
    # Join runs in the order of their first number; each run joins the longest chain
    # that ends one to four numbers before it.
    chains: list[list] = []
    by_end: dict[int, list[list]] = {}
    latest: dict[int, int] = {}                       # id(chain) -> its last position in the document
    # Runs starting at the same number: the longest claims the place first.
    for run in sorted(runs, key=lambda run: (run[0][1], -len(run), run[0][0].start())):
        first_number, first_position = run[0][1], run[0][0].start()
        best = None
        for gap in range(1, 5):
            for chain in by_end.get(first_number - gap, ()):
                # Joining backwards in the document is only for blocks extracted out of
                # order, never for one stray number ("12. März" inside entry 3).
                if first_position < latest[id(chain)] and len(run) < 3:
                    continue
                if min(len(run), len(chain)) < 2 and max(len(run), len(chain)) < 3:
                    continue
                if moved_on(chain[-1][0].start(), first_number, first_position):
                    continue
                rank = (kind(chain[-1][0]) == kind(run[0][0]), len(chain))
                if best is None or rank > best[2]:
                    best = (chain, first_number - gap, rank)
        if best:
            chain, end, _ = best
            by_end[end].remove(chain)
            chain.extend(run)
            latest[id(chain)] = max(latest[id(chain)], run[-1][0].start())
        else:
            chain = run
            chains.append(chain)
            latest[id(chain)] = run[-1][0].start()
        by_end.setdefault(run[-1][1], []).append(chain)
    return chains


def numbered_slots(text, start):
    """Printed entry numbers after the reference heading: the list's own numbers.

    A line can start with a number that is not an entry: a year ("2016."), an edition
    ("1. Aufl."), an issue ("(1) 23-45"), a date ("12. März"), an enumeration inside an
    annotation ("1) first point"). So:

    1. a marker counts as an entry only when an entry's worth of text follows it;
       short line starts are kept only to fill a hole between their neighbours, or
       as the list's first or last entry;
    2. entries form runs counting up by one, joined across small gaps — in any order,
       so a list extracted out of order stays whole;
    3. the longest run is the list. Another run is kept when it stands before or after
       the list and is long or starts again ("Further reading"), or when it is woven
       through the list densely (two columns read line by line). A run inside one
       entry ("1. Introduction", "2. Method") is an enumeration, never a list;
    4. the marker style ("[1]", "(1)", "1." or "1)") is the one whose runs keep the
       most entries. A list may start at any number (57, 58, …).
    """
    found = []
    for match in ENTRY_MARKER.finditer(text, start):
        number = int(re.sub(r"\D", "", match.group(1)) or 0)
        if 1 <= number <= 5000:
            found.append((match, number, _marker_style(match.group(1))))
    if not found:
        return []
    length = {}
    for index, (match, _, _) in enumerate(found):
        following = found[index + 1][0].start() if index + 1 < len(found) else len(text)
        length[id(match)] = len(re.sub(r"\s", "", text[match.end():min(following, match.end() + 400)]))
    # An entry's worth of text, relative to this list's own entries: "1. Aufl." among
    # forty-character entries is a line start, "[3] Ibid." among short ones is an entry.
    strong = {}
    for style in {kind for _, _, kind in found}:
        sizes = sorted(length[id(match)] for match, _, kind in found if kind == style)
        typical = sizes[len(sizes) // 2]
        for match, _, kind in found:
            if kind == style:
                strong[id(match)] = length[id(match)] >= max(2, 0.3 * typical)

    # What follows each marker, up to the next one: a reference names a year, a DOI or a link.
    entry_text = {id(match): text[match.end():found[index + 1][0].start() if index + 1 < len(found)
                                  else min(len(text), match.end() + 400)][:400]
                  for index, (match, _, _) in enumerate(found)}

    kinds = {key: bool(BIBLIOGRAPHIC.search(value)) for key, value in entry_text.items()}

    def bibliographic(run) -> bool:
        return sum(kinds[id(match)] for match, _ in run) * 2 >= len(run)

    def accepted(style):
        items = [(match, number) for match, number, kind in found if kind == style and strong[id(match)]]
        runs = _runs(items, kind=lambda match: kinds[id(match)])
        if not runs:
            return []
        main = max(runs, key=lambda run: (len(run), -min(m.start() for m, _ in run)))
        # A list of references names years; a numbered list after it that does not (the items
        # of a questionnaire in an appendix) is not a second list of references.
        references_only = bibliographic(main)
        main_first = min(number for _, number in main)
        anchors = sorted(m.start() for m, _ in main)
        kept = [main]
        for run in runs:
            if run is main:
                continue
            first = min(number for _, number in run)
            places = [bisect.bisect_left(anchors, m.start()) for m, _ in run]
            if min(places) == max(places) and 0 < places[0] < len(anchors):
                continue                                  # inside one entry: an enumeration
            restarts = first == main_first or first <= 3
            if references_only and not bibliographic(run):
                # Undated entries after dated references: archival sources ("Quellen", "Primary
                # sources") are a list; the items of a questionnaire in an appendix are not.
                # Such a list has a heading of its own on the line before it; points that go on
                # from the last entry ("1. Introduction …") have none.
                opening = min(m.start() for m, _ in run)
                heading = text[:opening].rstrip().rsplit("\n", 1)[-1].strip()
                titled = 0 < len(heading.split()) <= 6 and not heading.endswith((".", ",", ";")) \
                    and not APPENDIX.search(heading)
                if not titled or min(places) != max(places):
                    continue
            if min(places) == max(places):               # wholly before or after the list
                if len(run) >= 4 or (restarts and len(run) >= 2) or (first == main_first and places[0] > 0):
                    kept.append(run)
                continue
            # Woven through the list: a second column read line by line spreads over
            # neighbouring entries; stray line starts are scattered far apart.
            spread = len(set(places))
            dense = max(places) - min(places) <= 4 * len(run)
            if dense and spread >= 3 and (len(run) >= 4 or (restarts and len(run) >= 3)):
                kept.append(run)
        # Markers left over may fill a hole in the list where they sit among their
        # neighbours: a short line start between the entries before and after it, an
        # entry swapped with its neighbour within two places.
        used = {id(match) for run in kept for match, _ in run}
        spare = [(match, number) for match, number, kind in found if kind == style and id(match) not in used]
        others = sorted(match.start() for run in kept for match, _ in run)
        for run in kept:
            positions = {number: match.start() for match, number in run}
            numbers = sorted(positions)
            low, high = numbers[0], numbers[-1]
            for match, number in spare:
                if number in positions or not low < number < high:
                    continue
                reach = 2 if strong[id(match)] else 1
                index = bisect.bisect_right(numbers, number - reach) - 1
                before = numbers[index] if index >= 0 else None
                index = bisect.bisect_left(numbers, number + reach)
                after = numbers[index] if index < len(numbers) else None
                if before is not None and after is not None and positions[before] < match.start() < positions[after]:
                    run.append((match, number))
                    positions[number] = match.start()
                    bisect.insort(numbers, number)
            # A short last entry ("[4] Ibid., p. 4.") after the run's end, and a short
            # first entry ("[1] WHO (2020).") before its start, with no other entry between.
            end = numbers[-1]
            for match, number in sorted(spare, key=lambda item: item[0].start()):
                if number == end + 1 and match.start() > positions[end] and number not in positions:
                    run.append((match, number))
                    positions[number] = match.start()
                    end = number
            begin = numbers[0]
            for match, number in sorted(spare, key=lambda item: item[0].start(), reverse=True):
                if number == begin - 1 and match.start() < positions[begin] and number not in positions:
                    between = bisect.bisect_right(others, match.start()) < bisect.bisect_left(others, positions[begin])
                    if between:
                        break
                    run.append((match, number))
                    positions[number] = match.start()
                    begin = number
        return [match for run in kept for match, _ in run]

    choices = [accepted(style) for style in {kind for _, _, kind in found}]
    # The list is the style whose entries carry the most text; "1) first point"
    # enumerations number more lines than they fill.
    best = max(choices, key=lambda matches: (sum(length[id(match)] for match in matches), len(matches)))
    return sorted(best, key=lambda match: match.start())


def citation_occurrences(text, pages, digest):
    result = []
    for match in CITATION_PATTERN.finditer(text):
        start, end = match.span()
        # Source offsets, not generated context snippets, define occurrence identity.
        before = max(text.rfind("\n\n", 0, start), start - 350, 0)
        after = text.find("\n\n", end)
        after = min(after if after >= end else len(text), end + 350)
        context_text = text[before:after]
        context_start = before + len(context_text) - len(context_text.lstrip())
        context_end = after - (len(context_text) - len(context_text.rstrip()))
        locations = span_locations(pages, start, end)
        result.append({"occurrence_id": digest_text(f"{digest}:{start}:{end}"),
                       "marker": match.group(), "reference_keys": marker_ids(match.group()),
                       "context_window": text[context_start:context_end],
                       "target_marker_start": start - context_start, "target_marker_end": end - context_start,
                       "source_locations": locations,
                       "context_locations": span_locations(pages, context_start, context_end),
                       "location_status": "located"})
    return result


def bibliography_style(text):
    """("author_year" | "numbered" | "unknown", author–year sections).

    Author–year lists ("Wenger, E. (1998). …") are read deterministically. A
    numbered list ("1) Srikanth, B., … (2024)") keeps the numbered path even
    when its entries print years in parentheses: the numbers are what the text
    cites. One decision, shared by anchoring and the no-model fallback.
    """
    sections = bib.find_sections(text)
    boundary = reference_boundary(text)
    numbered = len(numbered_slots(text, boundary.end())) if boundary else 0
    author_year = sum(len(section.entries) for section in sections)
    if author_year >= 3 and (numbered < 3 or author_year >= 3 * numbered):
        return "author_year", sections
    return ("numbered" if numbered >= 3 else "unknown"), sections


def anchor_manuscript(parsed, source):
    """Model metadata is an observation; printed slots and positions remain authoritative."""
    result = copy.deepcopy(parsed)
    text, pages = joined_source(source)
    style, sections = bibliography_style(text)
    boundary = reference_boundary(text)
    if style == "author_year":
        return anchor_author_year(result, source, text, pages, sections)
    warnings = [w for w in result.get("extraction_metadata", {}).get("warnings", [])
                if w != "unassigned_model_reference_observations"]
    result.pop("unassigned_model_references", None)
    if boundary is None:
        warnings.append("references_boundary_unresolved")
    body_end = boundary.start() if boundary else len(text)
    if boundary is None:
        result["unlocated_model_citations"] = result.get("citations_in_text", [])
        result["citations_in_text"] = []
    else:
        result["citations_in_text"] = citation_occurrences(text[:body_end], pages, source["pdf_sha256"])
    observed_refs = result.get("references_list", [])
    by_key = {}
    for item in observed_refs:
        by_key.setdefault(canonical_id(item.get("ref_id")), []).append(item)
    slots = numbered_slots(text, boundary.end()) if boundary else []
    slot_counts = Counter(canonical_id(marker.group(1)) for marker in slots)
    anchored, matched = [], set()
    linked_observations = 0
    for index, marker in enumerate(slots):
        key = canonical_id(marker.group(1))
        end = slots[index + 1].start() if index + 1 < len(slots) else len(text)
        segment = text[marker.start():end]
        start = marker.start() + len(segment) - len(segment.lstrip())
        raw = segment.strip()
        observations = by_key.get(key, [])
        item = copy.deepcopy(observations[0]) if len(observations) == 1 else {"parsed": {}}
        matched.add(key)
        item["ref_id"] = f"[{key}]"
        item["reference_uid"] = digest_text(f"{source['pdf_sha256']}:{marker.start()}:{end}")
        item["raw_text"] = raw
        item["source_locations"] = span_locations(pages, start, start + len(raw))
        item["extraction_issues"] = []
        if len(observations) == 1:
            item["model_ref_id"] = observations[0].get("ref_id")
            if slot_counts[key] == 1:
                linked_observations += 1
        if slot_counts[key] > 1:
            item["extraction_issues"].append("duplicate_printed_reference_label")
        metadata = item.setdefault("parsed", {})
        title = metadata.get("title")
        if title and bibliographic_text(title) not in bibliographic_text(raw):
            item["model_observation"] = copy.deepcopy(metadata)
            metadata["title"] = None
            item["extraction_issues"].append("title_not_grounded_in_entry")
        from src.evidence.identity import surnames
        names = surnames(metadata.get("authors", []))
        if names and any(name not in bibliographic_text(raw) for name in names):
            item.setdefault("model_observation", copy.deepcopy(metadata))
            metadata["authors"] = []
            item["extraction_issues"].append("authors_not_grounded_in_entry")
        printed_doi = normalize_doi(metadata.get("doi")).strip() if metadata.get("doi") else ""
        if printed_doi and printed_doi not in normalize_doi(re.sub(r"\s+", "", raw)):
            # A DOI the model supplied but the entry does not print would make a correct
            # reference look like a fabricated one. It is kept only as an observation.
            item.setdefault("model_observation", copy.deepcopy(metadata))
            metadata["doi"] = None
            item.setdefault("dropped_model_fields", []).append("doi")
        if metadata.get("year") and str(metadata["year"]) not in raw:
            item.setdefault("model_observation", copy.deepcopy(metadata))
            metadata["year"] = None
            item["extraction_issues"].append("year_not_grounded_in_entry")
        if not metadata.get("title"):
            item["extraction_issues"].append("bibliography_fragment")
        if len(observations) != 1:
            item["extraction_issues"].append("missing_or_duplicate_model_entry")
        item["extraction_status"] = "needs_review" if item["extraction_issues"] else "located"
        anchored.append(item)
    if not slots:
        # Unnumbered bibliographies retain all model records and the original section.
        for index, observation in enumerate(observed_refs):
            item = copy.deepcopy(observation)
            item["reference_uid"] = digest_text(f"{source['pdf_sha256']}:unlocated:{index}")
            item["source_locations"] = []
            item["raw_text"] = ""
            item["extraction_status"] = "needs_review"
            item["extraction_issues"] = ["reference_entry_unlocated"]
            anchored.append(item)
        warnings.append("reference_entries_need_location_review")
    else:
        unassigned = [r for r in observed_refs if canonical_id(r.get("ref_id")) not in matched]
        if unassigned:
            result["unassigned_model_references"] = unassigned
            warnings.append("unassigned_model_reference_observations")
    ambiguities = []
    for previous, current in zip(anchored, anchored[1:]):
        if ("bibliography_fragment" in previous["extraction_issues"]
                and re.search(r"\b(?:19|20)\d{2}\b", current["raw_text"])
                and not re.search(r"\b(?:19|20)\d{2}\b", previous["raw_text"])):
            group = {"kind": "possible_split_entry", "reference_ids": [previous["ref_id"], current["ref_id"]],
                     "suggestion": "Inspect whether these printed entries describe one work. Original numbering is preserved."}
            ambiguities.append(group)
            for item in (previous, current):
                item["extraction_issues"].append("bibliography_boundary_ambiguous")
                item["extraction_status"] = "needs_review"
    result["references_list"] = anchored
    result["source_document"] = source
    metadata = result.setdefault("extraction_metadata", {})
    metadata.update({"method_version": EXTRACTION_VERSION, "warnings": list(dict.fromkeys(warnings)),
                     "linked_model_entries": linked_observations,
                     "unassigned_model_entries": len(result.get("unassigned_model_references", [])),
                     "entries_needing_review": sum(r["extraction_status"] != "located" for r in anchored),
                     "bibliography_ambiguities": ambiguities, "printed_slots": len(slots),
                     "retained_entries": len(anchored), "citation_occurrences": len(result["citations_in_text"]),
                     "reference_boundary": "heading" if boundary else "unresolved",
                     "reference_section_text": text[boundary.end():] if boundary else None})
    # A new extraction observation may correct metadata without rewriting the previous run.
    identity = {"source": source["pdf_sha256"], "method": EXTRACTION_VERSION,
                "references": anchored, "citations": result["citations_in_text"]}
    metadata["extraction_revision"] = digest_json(identity)
    return result


def _context(text, start, end):
    """The ±350-character window used for every citation, bounded by paragraphs."""
    before = max(text.rfind("\n\n", 0, start), start - 350, 0)
    after = text.find("\n\n", end)
    after = min(after if after >= end else len(text), end + 350)
    window = text[before:after]
    context_start = before + len(window) - len(window.lstrip())
    context_end = after - (len(window) - len(window.rstrip()))
    return context_start, context_end


def _model_match(entry, observations):
    """The model's record for a printed entry: same first author and year, or a grounded title."""
    surname, year = bib.fold(entry.first_surname), (entry.year or "")[:4]
    for observation in observations:
        parsed = observation.get("parsed") or {}
        authors = parsed.get("authors") or []
        first = bib.fold(re.split(r"[,\s]", str(authors[0]).strip())[0] if authors else "")
        if surname and first and (surname.startswith(first) or first.startswith(surname)) \
                and year and str(parsed.get("year") or "")[:4] == year:
            return observation
        title = bibliographic_text(parsed.get("title") or "")
        if title and len(title) > 15 and title in bibliographic_text(entry.raw):
            return observation
    return None


def anchor_author_year(result, source, text, pages, sections):
    """Anchor an author–year bibliography and its citations without trusting a model.

    Every printed entry becomes a reference with exact page locations; the
    model's observation (if any) may refine a title or author list only where
    its words appear in the printed entry. Citations are found by first-author
    surname and year in the text that belongs to each reference list, footnote
    citations are attributed to the superscript that calls them, and a
    citation whose year disagrees with the listed year is recorded.
    """
    from src.evidence.identity import surnames

    digest = source["pdf_sha256"]
    observations = list(result.get("references_list", []))
    warnings = [w for w in result.get("extraction_metadata", {}).get("warnings", [])
                if w != "unassigned_model_reference_observations"]
    result.pop("unassigned_model_references", None)
    multiple = len(sections) > 1
    anchored, occurrences, mismatches = [], {}, []
    used_observations = set()

    for number, section in enumerate(sections, 1):
        labels = bib.labels_for(section.entries)
        if multiple:
            labels = [f"{label} · ch. {number}" for label in labels]
        section_mismatches = []
        citations = bib.find_citations(text, section.body_start, section.heading_start, section.entries,
                                       labels, section_mismatches)
        items = []
        for entry, label in zip(section.entries, labels):
            observation = _model_match(entry, [o for i, o in enumerate(observations) if i not in used_observations])
            parsed = {"title": entry.title, "authors": entry.authors or ([entry.organization] if entry.organization else []),
                      "year": int(entry.year[:4]) if entry.year else None, "venue": None, "doi": entry.doi}
            item = {"ref_id": label, "parsed": parsed}
            issues = []
            if observation is not None:
                used_observations.add(observations.index(observation))
                model = observation.get("parsed") or {}
                item["model_ref_id"] = observation.get("ref_id")
                title = model.get("title")
                if title and bibliographic_text(title) in bibliographic_text(entry.raw) and len(title) > len(entry.title or ""):
                    parsed["title"] = title                 # the model kept a subtitle the rules cut
                names = surnames(model.get("authors") or [])
                if names and all(name in bibliographic_text(entry.raw) for name in names) and len(names) >= len(parsed["authors"]):
                    parsed["authors"] = model["authors"]
                if model.get("venue") and bibliographic_text(model["venue"]) in bibliographic_text(entry.raw):
                    parsed["venue"] = model["venue"]
            if not parsed["title"]:
                issues.append("bibliography_fragment")
            item.update({
                "reference_uid": digest_text(f"{digest}:{entry.start}:{entry.end}"),
                "raw_text": entry.raw,
                "source_locations": span_locations(pages, entry.start, entry.end),
                "extraction_issues": issues,
                "extraction_status": "needs_review" if issues else "located",
                "extraction_method": "deterministic_author_year",
                "bibliography": {"section": number, "heading": section.heading, "subsection": entry.subsection,
                                 "year_label": entry.year, "year_status": entry.year_status,
                                 "organization": entry.organization, "url": entry.url, "editors": entry.editors},
            })
            items.append(item)
        for mismatch in section_mismatches:
            for position in mismatch["entries"]:
                items[position].setdefault("citation_year_mismatches", []).append({
                    "cited_as": mismatch["marker"], "cited_year": mismatch["cited_year"],
                    "listed_year": section.entries[position].year or section.entries[position].year_status,
                    "source_locations": span_locations(pages, mismatch["start"], mismatch["end"])})
            mismatches.append({"section": number, "cited_as": mismatch["marker"], "cited_year": mismatch["cited_year"],
                               "listed": mismatch["listed"]})
        anchored.extend(items)

        for citation in citations:
            start, end = citation["start"], citation["end"]
            anchor_start, anchor_end, footnote = start, end, None
            page = next((p for p in pages if p["global_start"] <= start < p["global_end"]), None)
            if page is not None:
                call = bib.footnote_anchor(page["text"], start - page["global_start"])
                if call is not None:
                    anchor_start, anchor_end = page["global_start"] + call[0], page["global_start"] + call[1]
                    footnote = {"number": call[2], "citation_text": citation["marker"],
                                "locations": span_locations(pages, start, end)}
            key = (anchor_start, anchor_end)
            occurrence = occurrences.get(key)
            if occurrence is None:
                context_start, context_end = _context(text, anchor_start, anchor_end)
                occurrence = {
                    "occurrence_id": digest_text(f"{digest}:{anchor_start}:{anchor_end}"),
                    "marker": citation["marker"] if footnote is None else f"{citation['marker']} (footnote {footnote['number']})",
                    "reference_keys": [],
                    "context_window": text[context_start:context_end],
                    "target_marker_start": anchor_start - context_start,
                    "target_marker_end": anchor_end - context_start,
                    "source_locations": span_locations(pages, anchor_start, anchor_end),
                    "context_locations": span_locations(pages, context_start, context_end),
                    "location_status": "located",
                    "citation_style": "footnote" if footnote else "author_year",
                }
                if footnote:
                    occurrence["footnote"] = footnote
                occurrences[key] = occurrence
            elif footnote and citation["marker"] not in occurrence["marker"]:
                occurrence["marker"] = occurrence["marker"].replace(f" (footnote {footnote['number']})",
                                                                    f"; {citation['marker']} (footnote {footnote['number']})")
            key_id = canonical_id(citation["label"])
            if key_id not in occurrence["reference_keys"]:
                occurrence["reference_keys"].append(key_id)
            if citation.get("year_mismatch"):
                occurrence["year_mismatch"] = True

    ordered = [occurrences[key] for key in sorted(occurrences)]
    unassigned = [o for i, o in enumerate(observations) if i not in used_observations]
    if unassigned:
        result["unassigned_model_references"] = unassigned
    if multiple:
        warnings.append("multiple_bibliographies")
    if mismatches:
        warnings.append("citation_year_mismatches")
    result["citations_in_text"] = ordered
    result["references_list"] = anchored
    result["source_document"] = source
    metadata = result.setdefault("extraction_metadata", {})
    cited = {key for occurrence in ordered for key in occurrence["reference_keys"]}
    metadata.update({
        "method_version": EXTRACTION_VERSION, "warnings": list(dict.fromkeys(warnings)),
        "bibliography_style": "author_year",
        "bibliographies": [{"section": number, "heading": section.heading,
                            "pages": sorted({loc["page"] for loc in span_locations(pages, section.heading_start, section.end)}),
                            "body_pages": sorted({loc["page"] for loc in span_locations(pages, section.body_start, section.heading_start)}),
                            "entries": len(section.entries)} for number, section in enumerate(sections, 1)],
        "linked_model_entries": len(used_observations),
        "unassigned_model_entries": len(unassigned),
        "entries_needing_review": sum(item["extraction_status"] != "located" for item in anchored),
        "bibliography_ambiguities": [], "printed_slots": len(anchored), "retained_entries": len(anchored),
        "citation_occurrences": len(ordered),
        "footnote_citations": sum(o["citation_style"] == "footnote" for o in ordered),
        "cited_entries": sum(canonical_id(item["ref_id"]) in cited for item in anchored),
        "citation_year_mismatches": mismatches,
        "reference_boundary": "heading",
        "reference_section_text": "\n\n".join(text[s.heading_start:s.end] for s in sections),
    })
    identity = {"source": digest, "method": EXTRACTION_VERSION, "references": anchored, "citations": ordered}
    metadata["extraction_revision"] = digest_json(identity)
    return result
