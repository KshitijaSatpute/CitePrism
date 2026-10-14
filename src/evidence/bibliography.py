"""Deterministic reading of author–year bibliographies and their citations.

The model-based extractor handles numbered reference lists well, but most of
the humanities, the social sciences and German-language scholarship use
alphabetical author–year lists (APA, Harvard, DGPs) cited as "(Wenger, 1998)",
"Wenger (1998)" or in footnotes ("Vgl. Wenger, 1998"). This module reads such
documents without any model call:

* `find_sections` locates every bibliography in the document (multilingual
  headings; edited volumes and theses have one per chapter) and where each ends.
* `split_entries` turns a section into printed entries, ignoring running heads
  and page numbers, and `parse_entry` reads authors, year, title, DOI and URL.
* `find_citations` locates citations of those entries in the text that belongs
  to the section, linking each one by first-author surname and year. Because
  only (surname, year) pairs that exist in the bibliography are searched for,
  ordinary text such as "Bielefeld 2024" cannot become a citation by accident.

Everything is offset-based on the extracted page text, so each entry and each
citation carries exact page locations, like the rest of the evidence chain.
"""

from __future__ import annotations

import copy
import hashlib
import re
import threading
import unicodedata
from dataclasses import dataclass, field

from src.evidence.textclasses import NAME_CHAR, UPPER

# Headings that open a bibliography, in the languages CitePrism is used with.
OPENING = (
    r"references|reference\s+list|bibliography|works\s+cited|literature(?:\s+cited)?|sources|"
    r"literatur|literaturverzeichnis|literatur\s+und\s+quellen|quellen|quellenverzeichnis|"
    r"verwendete\s+literatur|zitierte\s+literatur|bibliografie|bibliographie|"
    r"références|références\s+bibliographiques|bibliografía|referencias|bibliografia|riferimenti(?:\s+bibliografici)?|"
    r"literatuur|referenties|litteratur|referenser"
)
SECTION_HEADING = re.compile(
    rf"(?im)^[ \t]*(?:(?:\d+(?:\.\d+)*[.)]?|[IVX]+[.)])[ \t]*)?(?:{OPENING})[ \t]*:?[ \t]*$"
)
# Sub-headings inside a bibliography that do not end it.
SUBHEADING = re.compile(
    r"(?i)^(?:internetquellen|online-?quellen|onlinequellen|weiterführende\s+literatur|weitere\s+literatur|"
    r"primärliteratur|sekundärliteratur|primärquellen|sekundärquellen|gesetze(?:\s+und\s+verordnungen)?|"
    r"rechtsquellen|online\s+sources|web\s+sources|further\s+reading|additional\s+reading|software|"
    r"datasets?|data\s+sources|primary\s+sources|secondary\s+sources|grey\s+literature|graue\s+literatur|"
    r"online\s+references|online-?referenzen|internet\s+(?:sources|references)|web\s*sites?|webseiten|links)\s*:?$"
)
# Headings that close a bibliography.
CLOSING = re.compile(
    r"(?i)^(?:abbildungen|abbildungsverzeichnis|tabellen|tabellenverzeichnis|abbildungs-\s*und\s+tabellenverzeichnis|"
    r"figures|list\s+of\s+figures|tables|list\s+of\s+tables|anhang|anhänge|appendix|appendices|"
    r"über\s+die\s+autor\S*|zu\s+den\s+autor\S*|autor\S*|about\s+the\s+authors?|notes|endnoten|anmerkungen|"
    r"danksagung|acknowledge?ments?|glossar|glossary|index|register|abstract|zusammenfassung)\s*:?$"
)

# "(1998)", "(1998a)", "(2020, 15. Juli)", "(o. J.)", "(n.d.)", "(i. E.)", "(in press)"
YEAR = re.compile(
    r"\(\s*(?:(?P<year>(?:1[5-9]|20)\d{2})(?P<suffix>[a-z])?(?:\s*[,/][^()]{0,40})?"
    r"|(?P<nodate>o\.\s?J\.|n\.\s?d\.|s\.\s?d\.|ohne\s+Jahr|no\s+date)"
    r"|(?P<press>i\.\s?E\.|i\.\s?Dr\.|im\s+Druck|in\s+press|forthcoming|in\s+Vorbereitung)"
    r"|(?:\d{1,2}\.\s?){1,2}(?P<dated>(?:19|20)\d{2}))\s*\)",
    re.IGNORECASE,
)
DOI = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
# What follows a printed DOI and is not part of it: access notes, PMIDs, brackets.
DOI_TAIL = re.compile(r"(?i)(?:\[|\(|zugriff|abgerufen|accessed|retrieved|pmid|epub|\.?$)")
URL = re.compile(r"(?:https?://|www\.)[^\s\"<>]+", re.IGNORECASE)
EDITOR_MARK = re.compile(r"\(\s*(?:hrsg\.?|hg\.?|eds?\.?|editors?|red\.?|coord\.?|dir\.?)\s*\)\.?", re.IGNORECASE)
PARTICLES = {"van", "von", "de", "der", "den", "du", "la", "le", "da", "dos", "das", "del", "di", "al", "zu", "ten",
             "ter", "und", "and", "für", "of", "the", "for", "et", "y", "e", "des", "in", "im", "am", "an", "auf", "zur",
             "zum", "fur", "aus", "bei"}
NAME = rf"{UPPER}{NAME_CHAR}+"
PERSON = re.compile(
    rf"(?P<surname>(?:(?:van|von|de|der|den|du|la|le|da|dos|del|di|ten|ter)\s+)*{NAME}(?:[\s\-]{NAME})?)"
    rf"\s*,?\s+(?P<initials>(?:{UPPER}\.?\s?-?\s?){{1,4}})(?=\s*(?:,|&|\(|$|und\b|and\b|u\.))"
)


def fold(text: str | None) -> str:
    """Case- and accent-insensitive key: 'MÅRTENSSON' and 'Mårtensson' agree."""
    value = unicodedata.normalize("NFKD", str(text or "")).casefold()
    return "".join(character for character in value if character.isalnum())


@dataclass
class Entry:
    raw: str
    start: int
    end: int
    authors: list[str] = field(default_factory=list)
    organization: str | None = None
    year: str | None = None
    year_status: str = "dated"          # dated | no_date | in_press
    title: str | None = None
    doi: str | None = None
    url: str | None = None
    editors: bool = False
    subsection: str | None = None

    @property
    def first_surname(self) -> str | None:
        if self.authors:
            return self.authors[0].split(",")[0].strip()
        return self.organization

    @property
    def key(self) -> tuple[str, str] | None:
        surname = fold(self.first_surname)
        return (surname, self.year) if surname and self.year else None


@dataclass
class Section:
    heading_start: int
    heading_end: int
    end: int
    heading: str
    body_start: int = 0
    entries: list[Entry] = field(default_factory=list)


# --------------------------------------------------------------------------- text
def _lines(text: str, start: int, end: int):
    """(line_start, line_end, line) within [start, end)."""
    position = start
    for line in text[start:end].split("\n"):
        yield position, position + len(line), line
        position += len(line) + 1


def _is_noise(line: str, page_top: bool = True) -> bool:
    """Page numbers and running heads ('35', 'COMMUNITY BUILDING', 'VOLK, BLUME').

    Upper-case lines count as running heads only at the top of a page; elsewhere
    they may be an organization author set in capitals.
    """
    stripped = line.strip()
    if not stripped:
        return True
    if re.fullmatch(r"\d{1,4}|[IVX]{1,5}\.?", stripped):
        return True
    if (page_top and len(stripped) <= 70 and not YEAR.search(stripped) and not re.search(r"\d", stripped)
            and _mostly_capitals(stripped) and not re.search(rf"\b{UPPER}\.", stripped)):
        return True
    return False


def _mostly_capitals(line: str) -> bool:
    """Upper-case set text, tolerating small-caps artefacts such as 'LERNF öRDERLICHES'."""
    letters = [character for character in line if character.isalpha()]
    return len(letters) >= 3 and sum(character.isupper() for character in letters) >= 0.85 * len(letters)


def _author_part_ok(author_text: str) -> bool:
    """Text before the year looks like authors or an organization, not a sentence.

    Rejects wrapped editor lists ("B. Szczyrba (Hrsg.), Handbuch … (S. 499–512)")
    and publisher lines ("NJ, London: Erlbaum.") that only reach a year by
    borrowing it from the next entry.
    """
    text = EDITOR_MARK.sub(" ", author_text).strip(" .,;:")
    if not text or len(text) > 320 or not re.match(UPPER, text):
        return False
    if re.match(rf"{UPPER}\.\s", text) or re.search(r"[:\d]", text) or re.search(r"\(S\.|\(pp?\.", text):
        return False
    words = re.findall(rf"{NAME_CHAR}+", text)
    lower = [word for word in words if word[0].islower() and word.lower() not in PARTICLES]
    return len(lower) <= 1


def _entry_start(text: str, line_start: int, line: str, following: str) -> bool:
    stripped = line.strip()
    if not stripped or _is_noise(stripped, page_top=False) or not re.match(UPPER, stripped):
        return False
    same_line = YEAR.search(stripped)
    if same_line:
        return _author_part_ok(stripped[:same_line.start()])
    # An author list may wrap onto the next line, but then this line cannot have
    # ended a sentence (an initial such as "M." is allowed).
    if stripped.endswith(".") and not re.search(rf"\b{UPPER}\.$", stripped):
        return False
    window = (stripped + " " + following.strip())[:360]
    match = YEAR.search(window)
    if not match or match.start() > 320:
        return False
    return _author_part_ok(window[:match.start()])


# ----------------------------------------------------------------------- sections
_SECTION_CACHE: dict[str, list[Section]] = {}
_SECTION_LOCK = threading.Lock()


def find_sections(text: str) -> list[Section]:
    """Every bibliography in the document, with where it ends.

    Style detection, anchoring and chapter detection all read the same text;
    the result is computed once per text and handed out as a copy.
    """
    key = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
    with _SECTION_LOCK:
        cached = _SECTION_CACHE.get(key)
    if cached is None:
        cached = _find_sections(text)
        with _SECTION_LOCK:
            while len(_SECTION_CACHE) >= 8:
                _SECTION_CACHE.pop(next(iter(_SECTION_CACHE)), None)
            _SECTION_CACHE[key] = cached
    return copy.deepcopy(cached)


def _find_sections(text: str) -> list[Section]:
    sections: list[Section] = []
    headings = list(SECTION_HEADING.finditer(text))
    for index, heading in enumerate(headings):
        limit = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        section = Section(heading.start(), heading.end(), limit, heading.group(0).strip())
        section.entries = split_entries(text, heading.end(), limit)
        if section.entries:
            section.end = section.entries[-1].end
            sections.append(section)
    previous_end = 0
    for section in sections:
        section.body_start = previous_end
        previous_end = section.end
    return sections


def split_entries(text: str, start: int, limit: int) -> list[Entry]:
    """Printed author–year entries between a heading and `limit`.

    Entry starts are the reliable signal: every other line continues the
    current entry. The list ends at a closing heading ("Abbildungen",
    "Appendix", …) or when running prose begins — the next chapter — and any
    such trailing lines are trimmed back to where the last entry was complete.
    """
    rows = list(_lines(text, start, limit))
    entries: list[Entry] = []
    current: list[tuple[int, int, str]] = []
    joined = ""           # the current entry's text, joined incrementally (linear, not cubic)
    complete_at = -1      # index in `current` of the last line an entry could end on
    prose_run = 0
    tainted = False       # a non-bibliographic line followed completion: nothing later extends the entry
    gap = 0               # blank or noise lines since the last content line
    since_blank = 99      # content lines since the last blank line (pages are joined by a blank line)
    subsection = None

    def close(upto: int | None = None):
        if current:
            kept = current if upto is None else current[:upto + 1]
            entry = _build_entry(text, kept, subsection) if kept else None
            if entry is not None:
                entries.append(entry)

    for index, (line_start, line_end, line) in enumerate(rows):
        stripped = line.strip()
        if not stripped:
            gap += 1
            since_blank = 0
            continue
        since_blank += 1
        if _is_noise(stripped, page_top=since_blank <= 2):
            gap += 1
            continue
        if CLOSING.match(stripped):
            break
        if SUBHEADING.match(stripped):
            close(); current, complete_at, prose_run, joined = [], -1, 0, ""
            subsection = stripped.rstrip(":")
            continue
        following = next((row[2] for row in rows[index + 1:index + 3]
                          if row[2].strip() and not _is_noise(row[2], page_top=False)), "")
        awaiting_year = bool(current) and not YEAR.search(joined)
        if not awaiting_year and _entry_start(text, line_start, line, following):
            close(complete_at if complete_at >= 0 else None)
            current, complete_at, prose_run, tainted, gap = [(line_start, line_end, line)], -1, 0, False, 0
            joined = _append("", stripped)
            if _ends_entry(stripped) and _substantial_text(joined):
                complete_at = 0
            continue
        if not current:
            continue                      # text between the heading and the first entry
        current.append((line_start, line_end, line))
        joined = _append(joined, stripped)
        joined_length = len(joined)
        broken_link = bool(re.search(r"(?:https?://|www\.|doi\.org/)\S*[/_\-]$", current[-2][2].rstrip()))
        if complete_at >= 0 and not (_bibliographic(stripped) or broken_link
                                     or (stripped[:1].islower() and gap < 3)):
            tainted = True        # a title block, or prose after a page gap, follows a complete entry
        gap = 0
        if not tainted and _ends_entry(stripped) and _substantial_text(joined):
            complete_at = len(current) - 1
            prose_run = 0
        elif complete_at >= 0 and _prose(stripped):
            prose_run += 1
        # Past a complete entry, three lines of running prose or an implausibly
        # long entry mean the bibliography has ended (the next chapter began).
        if complete_at >= 0 and (prose_run >= 3 or joined_length > 900):
            break
        if complete_at < 0 and joined_length > 2400:
            # An "entry" that never ends is a table or running text, not a reference:
            # keep only its opening line rather than swallowing the rest of the section.
            close(0)
            current, joined = [], ""
            break
    # Whatever follows the last point an entry could end is not part of it.
    close(complete_at if complete_at >= 0 else None)
    return entries


def _ends_entry(line: str) -> bool:
    """A line an entry can end on: a final period, or a bare URL or DOI."""
    line = line.rstrip()
    return line.endswith((".", ".)", "]")) or bool(re.search(r"(?:doi\.org/|https?://|www\.)\S+$", line, re.I))


BIBLIOGRAPHIC = re.compile(
    r"(?:https?://|www\.|doi|\bIn[: ]|\bin:|abgerufen|retrieved|verfügbar|available|zugriff|"
    r"\b(?:S|pp?|Bd|Vol|Heft|Nr|Aufl|ed|eds|Hrsg|Hg)\.|\d+\(\d+[^)]*\)|\d+\s?[–-]\s?\d+|"
    rf"^{UPPER}[\w .,&/-]{{1,60}}:\s*{UPPER})",
    re.IGNORECASE,
)


def _bibliographic(line: str) -> bool:
    """A line that can follow a complete entry: URL, DOI, venue, publisher, pages, access date."""
    return bool(BIBLIOGRAPHIC.search(line)) and not _prose(line) or bool(URL.search(line) or DOI.search(line))


def _substantial(lines: list[str]) -> bool:
    """The entry has text after its year: "(2021)." alone is not a complete entry."""
    return _substantial_text(_join(lines))


def _substantial_text(joined: str) -> bool:
    match = YEAR.search(joined)
    return bool(match) and len(joined[match.end():].strip(" .:")) >= 20


def _prose(line: str) -> bool:
    """Running text rather than a bibliography line: long, many lower-case words."""
    if len(line) < 45 or URL.search(line) or DOI.search(line) or YEAR.search(line):
        return False
    words = re.findall(r"[^\W\d_]+", line)
    return len(words) >= 7 and sum(word[0].islower() for word in words) >= 4


def _append(text: str, part: str) -> str:
    """`text` with one more printed line joined on. Looks only at the last token of
    `text`, so joining a whole entry line by line stays linear."""
    part = " ".join(part.split())
    if not part:
        return text
    if not text:
        return part
    last = text[text.rfind(" ") + 1:]
    if text.endswith("-") and part[:1].islower() and not text.endswith(" -"):
        return text[:-1] + part                  # German hyphenation: "zwi-\nschen"
    if text.endswith(("/", "_", "-")) or re.search(r"(?:https?://|www\.)\S*$", last) and not part[:1].isupper():
        return text + part                       # a broken URL or DOI
    return text + " " + part


def _join(parts: list[str]) -> str:
    text = ""
    for part in parts:
        text = _append(text, part)
    return text.strip()


def _build_entry(text: str, rows, subsection) -> Entry | None:
    raw = _join([row[2] for row in rows])
    year = YEAR.search(raw)
    if not year:
        return None
    entry = Entry(raw=raw, start=rows[0][0] + (len(rows[0][2]) - len(rows[0][2].lstrip())),
                  end=rows[-1][1], subsection=subsection)
    parse_entry(entry, raw, year)
    return entry


def parse_entry(entry: Entry, raw: str, year_match=None) -> Entry:
    year_match = year_match or YEAR.search(raw)
    author_text = raw[:year_match.start()].strip()
    entry.editors = bool(EDITOR_MARK.search(author_text))
    author_text = EDITOR_MARK.sub(" ", author_text).strip(" .,;:")
    author_text = re.sub(r"\s+([.,])", r"\1", author_text)          # PDF artefact: "FLEPP , G."
    if year_match.group("year"):
        entry.year = year_match.group("year") + (year_match.group("suffix") or "")
    elif year_match.group("dated"):
        entry.year = year_match.group("dated")
    elif year_match.group("nodate"):
        entry.year_status = "no_date"
    else:
        entry.year_status = "in_press"
    people = [f"{_titlecase(m.group('surname'))}, {_initials(m.group('initials'))}"
              for m in PERSON.finditer(author_text)]
    full = re.fullmatch(rf"({NAME}(?:[\s\-]{NAME})?),\s+({NAME}(?:\s+{NAME})?)", author_text)
    if people:
        entry.authors = people
    elif full:                                   # "PETERS, JONATHAN": a given name, not initials
        entry.authors = [f"{_titlecase(full.group(1))}, {_titlecase(full.group(2))}"]
    else:
        entry.organization = _titlecase(author_text.strip(" .")) or None
    rest = raw[year_match.end():].lstrip(" .:")
    title = re.split(rf"(?<=[^A-Z\s.]\w)[.?!]\s+(?=(?:In[: ]|{UPPER}|[0-9„“\"]))", rest, maxsplit=1)[0]
    entry.title = title.strip(" .") or None
    entry.doi = find_doi(raw)
    url = URL.search(raw)
    if url:
        entry.url = url.group(0).rstrip(".,;)")
    return entry


DOI_START = re.compile(r"(?i)(?<![\d.])10\.\d{4,9}\s?/|(?:doi\s*:?\s*|doi\.org/)10\.\s\d{4,9}\s?/")
# "Heft 10. 2008/09", "Bd. 10, 2013/2014": a volume and a year range, never a DOI.
YEAR_RANGE_DOI = re.compile(r"10\.(?:1[89]|20)\d\d/(?:\d{2}|\d{4})")
# Without a "doi" cue, a "prefix" that is a year with a bare number or word after it is an
# issue or a path: "Heft 10.2019/3", "10.2019/2020(3)", "…/news/10.2019/article".
YEAR_LIKE_DOI = re.compile(r"(?i)10\.(?:1[89]|20)\d\d/(?:\d{1,4}(?:\(\d{1,3}\))?|[a-z]+)")


NUMBERED_SEGMENTS = {"zenodo", "figshare", "osf", "dryad", "arxiv", "ssrn", "biorxiv", "medrxiv", "psyarxiv",
                     "socarxiv", "pone", "pbio", "pcbi", "pgen", "ppat", "pmed", "pntd", "pdig", "pgph", "pclm", "pwat",
                     "pstr", "pcsy", "f1000research", "elife", "peerj"}


def _continues_doi(value: str, piece: str, following: str = "") -> bool:
    """Whether `piece`, printed after a line break or space, is the rest of the DOI `value`.

    `following` is the text after `piece`, for the one case that needs it: a postcode
    ("10115 Berlin") is five digits like a short record number.
    """
    if DOI_TAIL.match(piece) or not re.match(r"[\w(<]", piece):
        return False
    if value.endswith("/"):
        return True                                   # nothing after the prefix yet: it must continue
    if value.endswith(("-", "_")):
        return bool(re.search(r"\d", piece) or piece[:1].islower())
    if value.endswith("."):
        # The rest of a DOI broken after a dot is a code: "jss.2020.110" or "JSS.2020.110"
        # (letters, a dot, digits), "2016.90", "2019.1234567", or a number ("zenodo." + "59176",
        # "pone." + "0172345", "5." + "726791"). Never a sentence word ("Retrieved", "u.a."), a
        # year ("2019,"), an issue ("12(3)"), an elocator ("e0172345"), a link, another
        # identifier ("arXiv:", "hdl:", "PMID"), an edition ("2nd"), a volume or page
        # ("Vol.12", "S.12-15"), a date ("12.03.2021"), a version ("v2.1"), a short number
        # ("3.5") or a postcode ("10115 Berlin").
        core = piece.rstrip(".,;")
        segment = re.split(r"[/.]", value.rstrip("."))[-1].lower()
        # Wiley ends DOIs with ".x" ("…02017.x"); the Open Science Framework uses "osf.io/…".
        if re.fullmatch(r"[xX]", core) and re.search(r"\d", segment):
            return True
        if segment == "osf" and re.match(r"io/\w+", core):
            return True
        if re.search(r"[:(),]|://|^www\.|^10\.|^doi", core, re.IGNORECASE) or not re.search(r"\d", core):
            return False
        if re.match(r"(?:pmid|pmcid|pmc|isbn|issn|arxiv|hdl|urn|orcid)\b", core, re.IGNORECASE):
            return False
        if re.match(r"(?:vol|nr|no|bd|jg|jahrg|heft|hft|iss|issue|art|chap|kap|fig|tab|suppl|ed|aufl|pp?|s)\.",
                    core, re.IGNORECASE):
            return False
        if re.fullmatch(r"(?:0?[1-9]|[12]\d|3[01])\.(?:0?[1-9]|1[0-2])\.(?:\d{2}|(?:19|20)\d{2})?", core) \
                or re.fullmatch(r"v\d+(?:\.\d+)*", core, re.I):
            return False
        # After a number or a numbered code ("2016.", "05.", "v34i04."), digits go on the DOI:
        # "CVPR.2016." + "90", "j.chb.2016.05." + "083", "aaai.v34i04." + "5953" — except a
        # publication year followed by a comma or bracket ("…. 2019, in press").
        rest = piece[len(core):]
        if re.fullmatch(r"\d{1,2}", core) and (rest.startswith(".,") or rest.startswith(".") and re.match(
                r"\s*(?:überarb|erw|neu|aufl|auflage|ausg|ed\b|edition|jahrg|jg|bd|band|kap|heft|h\.)", following, re.I)):
            return False                                  # an edition: "3., überarb. Aufl."
        # Never a count of pages ("120 S.", "12 p."), a numbered entry ("12. Smith, J.") or a
        # year standing alone ("2019." after the DOI).
        if re.fullmatch(r"\d{1,4}", core) and re.match(r"\s*(?:p|pp|s|seiten|pages|bl)\b(?![.\s]*\d)", following,
                                                        re.IGNORECASE):
            return False
        if re.fullmatch(r"\d{1,4}", core) and rest.startswith(".") and re.match(
                r"\s+[A-ZÄÖÜ][\w'’-]+(?:,\s*[A-ZÄÖÜ]|\s+[A-Z]{1,3}\b)", following):
            return False
        if re.search(r"\d", segment) and re.fullmatch(r"\d+(?:\.\d+)*", core) and (
                segment.isdigit() or len(re.sub(r"\D", "", core)) >= 2):
            if re.fullmatch(r"(?:19|20)\d\d", core) and (re.match(r"[,)\]]", rest or following[:1]) or not following.strip()
                                                         or rest.startswith(".")):
                return False
            place = re.match(r"\s+([A-ZÄÖÜ][a-zäöüß]{2,})\b(?!,\s*[A-ZÄÖÜ]\.)", following)
            return not (len(core) == 5 and place is not None and place.group(1).lower() not in NOT_PLACES)
        # A journal's short code before the dot ("nn.", "bmj.", "jc.", "acl-main.") is followed
        # by its article number ("nn.4504", "bmj.m1234", "jc.2011-1234", "acl-main.463").
        journal_code = bool(re.fullmatch(r"[a-z][a-z-]{1,9}", segment))
        if re.fullmatch(r"\d+", core):
            if len(core) < 5:
                return journal_code and len(core) >= 3 and not re.fullmatch(r"(?:1[5-9]|20)\d\d", core)
            # A postcode is five digits before a place name ("10115 Berlin") — not before the next
            # entry's author ("Smith, J.") or a note ("Retrieved …"), never in a registry's numbering.
            place = re.match(r"\s+([A-ZÄÖÜ][a-zäöüß]{2,})\b(?!,\s*[A-ZÄÖÜ]\.)", following)
            postcode = (len(core) == 5 and segment not in NUMBERED_SEGMENTS and place is not None
                        and place.group(1).lower() not in NOT_PLACES)
            return not postcode
        if re.fullmatch(r"[\d.]+", core):
            return len(re.sub(r"\D", "", core)) >= 3          # "2016.90", "2016.05.083", "1.29"
        if re.fullmatch(r"\d+(?:st|nd|rd|th)", core, re.IGNORECASE):
            return False
        if journal_code and (re.fullmatch(r"(?:19|20)\d\d-\d{3,}", core) or re.fullmatch(r"[a-df-z]\d{3,}", core, re.I)):
            return True                                   # "jc." + "2011-1234", "bmj." + "m1234"
        if re.fullmatch(r"[a-z0-9]*\d[a-z]+\d[a-z0-9]*", core, re.IGNORECASE):
            return True                                   # codes: "est." + "9b01234", "dryad." + "x0k6djhh5"
        if re.match(r"[A-Z]", core) and (re.search(r"[a-z]", core) or not re.match(r"[A-Z]{2,}[A-Z0-9]*\.", core)):
            return False                                  # "Vol.12", "S.12-15"; "JSS.2020.110" continues
        return bool(re.match(r"[A-Za-z0-9]", core) and "." in core)
    return False


NOT_PLACES = {"retrieved", "available", "accessed", "abgerufen", "zugriff", "zugegriffen", "in", "online", "published",
              "epub", "doi", "pmid", "isbn", "url", "verfügbar", "aufgerufen", "erschienen", "see", "siehe", "vgl"}


def find_doi(raw: str) -> str | None:
    """The DOI printed in an entry, without the text that follows it.

    The DOI ends at whitespace — unless the PDF broke it there: after "/", "-",
    "_" or "." the continuation is rejoined when it looks like DOI text
    ("10.1007/ 978-3-658-44153-1_3", "10.1016/j. jss.2020.110", "10. 1007/…").
    Access notes such as "[Zugriff: 12.03.2021]", "(accessed …)" or "PMID: …"
    are never glued on.
    """
    match = DOI_START.search(raw)
    if not match:
        return None
    value, position = re.sub(r"\s", "", match.group(0)), match.end()
    value = value[value.lower().index("10."):]          # drop the "doi:" / "doi.org/" cue
    for _ in range(12):                               # a DOI is at most a few printed pieces
        token = re.match(r"[^\s\"]*", raw[position:]).group(0)
        value, position = value + token, position + len(token)
        following = re.match(r"\s+([^\s\"]+)", raw[position:])
        if not following or not _continues_doi(value, following.group(1), raw[position + following.end():]):
            break
        position += following.start(1)
    # "[Zugriff …]" ends the DOI; a SICI's own "[0001:TTOTSA]" does not.
    value = re.split(r"(?i)\[(?!\d+:\w+\])|zugriff|abgerufen|accessed|retrieved", value)[0]
    if ">" in value and "<" not in value:
        value = value.split(">", 1)[0]          # "<https://doi.org/10.1000/abc123>"
    while value.endswith(">") and value.count(">") > value.count("<"):
        value = value[:-1]                      # "<doi:10.1002/(SICI)…<693::AID-ASI4>3.0.CO;2-0>"
    while value and (value[-1] in ".,;:-…»«”“’‘(" or (value.endswith("]") and value.count("[") < value.count("]"))
                     or (value.endswith(")") and value.count("(") < value.count(")"))):
        value = value[:-1]
    if YEAR_RANGE_DOI.fullmatch(value):
        return None
    cue = re.search(r"(?i)doi", raw[max(0, match.start() - 12):match.end()])
    if not cue and YEAR_LIKE_DOI.fullmatch(value):
        return None                             # "Heft 10.2019/3", ".../news/10.2019/article"
    return value.lower() if re.fullmatch(r"10\.\d{4,9}/\S+", value) else None


def _initials(value: str) -> str:
    """'J', 'J.', 'J. R', 'JR' -> 'J.', 'J.R.' — one consistent form for every author."""
    letters = re.findall(UPPER, value)
    return "".join(f"{letter}." for letter in letters)


def _capitalize(word: str) -> str:
    """'MCDERMOTT' -> 'McDermott', "O'BRIEN" -> "O'Brien", 'MÅRTENSSON' -> 'Mårtensson'."""
    word = word.capitalize()
    word = re.sub(r"^(Mc)([^\W\d_])", lambda m: m.group(1) + m.group(2).upper(), word)
    return re.sub(r"^([^\W\d_]['’])([^\W\d_])", lambda m: m.group(1) + m.group(2).upper(), word)


def _titlecase(value: str) -> str:
    """Names printed in capitals, in normal case; mixed-case names are kept as printed.
    Short capitalised words in parentheses are acronyms and stay as they are: 'Joint
    Research Centre (JRC)'."""
    value = re.sub(r"\s+", " ", value).strip()
    if value.upper() != value:
        return value
    acronyms = {match.group(1) for match in re.finditer(r"\(([^\W\d_]{2,6})\)", value)}

    def word(match):
        text = match.group(0)
        if text in acronyms:
            return text
        return text.lower() if text.lower() in PARTICLES else _capitalize(text)
    return re.sub(r"(?:[^\W\d_]|['’])+", word, value)


# ----------------------------------------------------------------------- citations
def labels_for(entries: list[Entry]) -> list[str]:
    """Readable, unique labels: 'Wenger 1998', 'Blume 2024a', 'Akademie … o. J.'."""
    labels, seen = [], {}
    for entry in entries:
        name = entry.first_surname or "Anonymous"
        name = name if len(name) <= 40 else name[:37].rstrip() + "…"
        when = entry.year or ("o. J." if entry.year_status == "no_date" else "i. E.")
        label = f"{name} {when}"
        seen[label] = seen.get(label, 0) + 1
        labels.append(label if seen[label] == 1 else f"{label} ({seen[label]})")
    return labels


UNDATED_CITED = r"o\.\s?J\.|n\.\s?d\.|i\.\s?E\.|in\s+press|im\s+Druck|forthcoming"
CITED_YEAR = re.compile(rf"(?P<year>(?:1[5-9]|20)\d{{2}})(?P<suffix>[a-z])?\b|(?P<undated>{UNDATED_CITED})", re.I)
# Between a cited surname and its year: co-authors, "et al.", "u. a.", "&", particles.
# Read token by token (never with a nested-quantifier regex, which backtracks
# exponentially on text such as "Smith and and and …").
CONNECTOR = re.compile(r"\s*(?:et\.?\s*al\.?|u\.\s?a\.|&|\bund\b|\band\b|,)\s*", re.IGNORECASE)
PARTICLE = re.compile(r"\s+(?:van|von|de|der|den|du|da|la|le|del|di|dos|ten|ter|zu)\b", re.IGNORECASE)
NAME_TOKEN = re.compile(rf"\s*(?<![\w'’\-]){UPPER}{NAME_CHAR}+")
YEAR_LEAD = re.compile(r"[\s,(]*")
WORD = re.compile(rf"(?<![\w'’\-]){UPPER}{NAME_CHAR}+")


def _past(body: str, start: int, folded: str) -> int:
    """Where the name whose folded form is `folded` ends, reading body from `start`.
    Organization names contain lower-case words ("Stiftung Innovation in der
    Hochschullehre") that the co-author tokens must not have to cross."""
    seen, index = 0, start
    while index < len(body) and seen < len(folded):
        seen += len(fold(body[index]))
        index += 1
    return index


def _year_after(body: str, position: int, limit: int):
    """The cited year (or o. J. / n. d. / i. E.) that follows a surname, read across
    co-authors and connectors, or None. Linear in the length of the window."""
    index = position
    while index <= limit:
        lead = YEAR_LEAD.match(body, index, limit)
        when = CITED_YEAR.match(body, lead.end(), limit)
        if when:
            return when
        for token in (CONNECTOR, PARTICLE, NAME_TOKEN):
            step = token.match(body, index, limit)
            if step and step.end() > index:
                index = step.end()
                break
        else:
            return None
    return None


def _aliases(entry: Entry) -> list[str]:
    """Search keys for an entry: its surname or organization, plus any acronym it prints, e.g. (JRC)."""
    keys = []
    if entry.first_surname:
        keys.append(entry.first_surname)
    if entry.organization:
        keys += re.findall(rf"\(({UPPER}(?:[^\W\d_]|[&.\-]){{1,9}})\)", entry.raw[:200])
    return keys


def find_citations(text: str, start: int, end: int, entries: list[Entry], labels: list[str],
                   mismatches: list | None = None) -> list[dict]:
    """Author–year citations of `entries` in text[start:end].

    A citation is a first-author surname (or an organization or its acronym)
    followed, across co-author names, 'et al.' / 'u. a.' and punctuation, by a
    year or by 'o. J.' / 'n. d.' / 'i. E.'. A name preceded by '&', 'und',
    'and' or a comma is a co-author, not a citation start, so
    "Axelsson, Blume, Volk, 2023" cites Axelsson only.

    Year suffixes follow APA order: "Koch, 2024b" is the second "Koch 2024" in
    the list when the list itself prints no suffixes. When the surname is in
    the bibliography but no entry has the cited year, the citation is linked to
    that author's only entry (if there is exactly one) and the disagreement is
    appended to `mismatches` — a citation-versus-bibliography inconsistency.
    """
    index: dict[str, list[tuple[str, int]]] = {}
    # Every author printed in the list: a capitalised word before a comma is a
    # co-author only if it is one of these ("Axelsson, Blume, Volk, 2023"), not
    # when it is ordinary text ("In Germany, Smith (2010)").
    authors = {fold(author.split(",")[0]) for entry in entries for author in entry.authors}
    for position, entry in enumerate(entries):
        for name in _aliases(entry):
            full = fold(name)
            first = fold(re.findall(rf"{UPPER}[\w'’\-]*", name)[0]) if re.search(UPPER, name) else full
            if full:
                index.setdefault(first, []).append((full, position))
                if full != first:      # "FEL TEN" (broken spacing) is cited as "Felten"
                    index.setdefault(full, []).append((full, position))
    found: list[dict] = []
    if not index:
        return found
    body = text[start:end]
    for match in WORD.finditer(body):
        candidates = index.get(fold(match.group(0)))
        if not candidates:
            continue
        # A co-author follows another name ("Hattie & Timperley", "Axelsson, Blume");
        # "…feedback, and Carless and Boud (2018)" starts a new citation, and so does
        # "In Germany, Smith (2010)".
        before = body[max(0, match.start() - 60):match.start()]
        joined = re.search(r"((?:[^\W\d_]|['’\-.])+)\s*(,\s*(?:and|&|und)?|&|\bund|\band)\s*$", before,
                           re.IGNORECASE)
        if joined and joined.group(1)[:1].isupper() and joined.group(1).lower() not in {"vgl.", "see", "siehe", "cf.",
                                                                                         "vgl", "see also"}:
            connector = joined.group(2).replace(",", "").strip().lower()
            if connector in {"&", "und", "and"} or fold(joined.group(1)) in authors:
                continue
        folded_ahead = fold(body[match.start():match.start() + 80])
        candidates = [(full, position) for full, position in candidates if folded_ahead.startswith(full)]
        if not candidates:
            continue
        longest = max(len(full) for full, _ in candidates)
        candidates = [(full, position) for full, position in candidates if len(full) == longest]
        name_end = max(match.end(), _past(body, match.start(), candidates[0][0]))
        when = _year_after(body, name_end, min(len(body), name_end + 120))
        if not when:
            continue
        targets = _resolve(entries, [position for _, position in candidates], when)
        absolute_start, absolute_end = start + match.start(), start + when.end()
        if "(" in body[match.end():when.end()] and text[absolute_end:absolute_end + 1] == ")":
            absolute_end += 1                       # "Wenger (1998)": include the closing parenthesis
        marker = " ".join(text[absolute_start:absolute_end].split())
        if targets is None:
            intended, distance = _nearest(entries, sorted({p for _, p in candidates}), when)
            if mismatches is not None:
                listed = sorted({entries[p].year or entries[p].year_status for p in intended})
                mismatches.append({"marker": marker, "start": absolute_start, "end": absolute_end,
                                   "cited_year": when.group(0), "listed": listed, "entries": intended})
            # Linked when one entry is the clear intent: the author's only work, or the
            # one a year away ("Blume, 2022" for Blume 2023 when 2024 and 2025 also exist).
            if len(intended) != 1 or (len({p for _, p in candidates}) > 1 and distance > 1):
                continue
            targets, mismatch = intended, True
        else:
            mismatch = False
        for target in targets:
            found.append({"start": absolute_start, "end": absolute_end, "entry": target, "label": labels[target],
                          "marker": marker, "year_mismatch": mismatch, "ambiguous": len(targets) > 1})
    return found


def _nearest(entries: list[Entry], positions: list[int], when) -> tuple[list[int], float]:
    """The entries a citation with a non-matching year most likely means, and how far off it is.

    One typo should be one finding: "Blume, 2022" against Blume 2023, 2024 and
    2025 points at 2023, not at all three. A matching suffix ("2025b" for 2024b)
    counts, an in-press entry is fairly close to any year, and ties keep every
    tied entry so nothing is hidden.
    """
    year = when.group("year")
    if not year:
        return positions, 0.0                  # "o. J." against dated entries: no year to compare
    suffix = (when.group("suffix") or "").lower()
    scored = []
    for position in positions:
        entry = entries[position]
        printed = re.match(r"(\d{4})([a-z]?)", entry.year or "")
        if printed:
            distance = abs(int(printed.group(1)) - int(year)) - (0.25 if suffix and printed.group(2) == suffix else 0)
        else:
            # In press: plausibly any recent year, but never closer than a one-year typo.
            distance = 1.5 if entry.year_status == "in_press" else 2.0
        scored.append((distance, position))
    best = min(distance for distance, _ in scored)
    return [position for distance, position in scored if distance == best], best


def _resolve(entries: list[Entry], positions: list[int], when) -> list[int] | None:
    """Entries a cited year points to, or None when none of this author's entries has it."""
    if when.group("undated"):
        token = re.sub(r"\s", "", when.group("undated").lower())
        status = "no_date" if token in {"o.j.", "n.d."} else "in_press"
        hits = [p for p in positions if entries[p].year_status == status]
        return hits or None
    year, suffix = when.group("year"), (when.group("suffix") or "").lower()
    exact = [p for p in positions if (entries[p].year or "") == year + suffix]
    if exact:
        return exact
    same_year = [p for p in positions if (entries[p].year or "")[:4] == year]
    if not same_year:
        return None
    if suffix:
        unsuffixed = [p for p in same_year if (entries[p].year or "") == year]
        order = ord(suffix) - ord("a")
        if unsuffixed and 0 <= order < len(unsuffixed):
            return [unsuffixed[order]]       # APA: a, b, … follow list order
        return None
    return same_year                          # "2024" against 2024a and 2024b: ambiguous, link both


FOOTNOTE_LINE = re.compile(r"(?m)^[ \t]*(\d{1,3})[ \t]+(?=\S)")


CALL = re.compile(r"(?:(?<=[^\W\d_])|(?<=[.,;:!?)“”\"'’]))( ?)(\d{1,3})(?!\d)(?=\s|$)")
_PAGE_INDEX: dict[int, tuple[str, list, dict]] = {}


def _page_index(page_text: str):
    """Footnote lines and candidate superscript calls of one page, scanned once.

    Every footnote on a page asks the same questions of the same text; scanning
    it once keeps a page with many footnotes linear instead of quadratic.
    """
    key = hash(page_text)
    cached = _PAGE_INDEX.get(key)
    if cached and cached[0] is page_text:
        return cached[1], cached[2]
    lines = [(match.start(), int(match.group(1))) for match in FOOTNOTE_LINE.finditer(page_text)]
    calls: dict[int, list[tuple[int, int, bool]]] = {}
    for match in CALL.finditer(page_text):
        calls.setdefault(int(match.group(2)), []).append((match.start(2), match.end(2), not match.group(1)))
    if len(_PAGE_INDEX) > 256:
        _PAGE_INDEX.clear()
    _PAGE_INDEX[key] = (page_text, lines, calls)
    return lines, calls


def footnote_anchor(page_text: str, offset: int) -> tuple[int, int, int] | None:
    """Where the footnote containing `offset` is called from in the body text.

    Footnote citations ("³ Vgl. Barnat, 2021.") support the sentence carrying the
    superscript, not the footnote area. Returns (start, end, number) of that
    superscript within `page_text`, or None when `offset` is not in a footnote
    or its call cannot be found on the same page.
    """
    line_start = page_text.rfind("\n", 0, offset) + 1
    number, footnote_start = None, None
    for _ in range(6):                                   # a footnote may wrap over a few lines
        match = FOOTNOTE_LINE.match(page_text, line_start)
        if match:
            number, footnote_start = int(match.group(1)), line_start
            break
        if line_start == 0:
            return None
        line_start = page_text.rfind("\n", 0, line_start - 1) + 1
    if number is None or number == 0:
        return None
    lines, calls = _page_index(page_text)
    # The body ends where the page's first footnote (number <= ours) begins.
    floor = int(len(page_text) * 0.3)
    body_end = next((start for start, value in lines if start >= floor and value <= number and start < footnote_start),
                    footnote_start)
    # A superscript is set against the word or punctuation it follows ("learning.3");
    # a number after a space ("run in 3 schools") is ordinary text, unless no attached
    # call exists — many PDFs extract superscripts with spaces ("kann. 7 Beide").
    candidates = [call for call in calls.get(number, []) if call[1] <= body_end]
    attached = [call for call in candidates if call[2]]
    chosen = (attached or candidates)[-1] if candidates else None
    if chosen is None:
        return None
    return chosen[0], chosen[1], number


def summary(sections: list[Section]) -> dict:
    entries = [entry for section in sections for entry in section.entries]
    return {
        "sections": len(sections),
        "entries": len(entries),
        "dated": sum(entry.year is not None for entry in entries),
        "with_doi": sum(entry.doi is not None for entry in entries),
        "organizations": sum(entry.organization is not None for entry in entries),
    }


__all__ = ["Entry", "Section", "find_citations", "find_doi", "footnote_anchor", "find_sections", "fold", "labels_for",
           "parse_entry", "split_entries", "summary"]
