"""Bibliographic metadata consistency between a printed entry and its source.

Phase 2 compared year and DOI only. CitePrism V2.0 asks the system to
"highlight metadata inconsistencies", and the ones reviewers actually meet are
wider: a mistyped or invented DOI, a misspelled title, a wrong first author,
a wrong venue, or a source that was later retracted.

Each difference carries a severity so the review queue can rank them:

``major``  likely to send a reader to the wrong work, or not to a work at all
           (DOI mismatch or unregistered DOI, first-author mismatch, a title
           that is clearly different, a year more than one apart).
``minor``  worth correcting, rarely misleading (title typos, an off-by-one
           year from online-first versus print, venue abbreviations).
``info``   about the database record, not the entry: a record that lists only
           some of the printed authors. Reported as a note, never ranked.

Comparisons are made only against a *resolved* source. An unresolved reference
has nothing trustworthy to be inconsistent with; identity review comes first.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from src.evidence.self_citation import person_key, same_surname, surname_variants
from src.evidence.text import bibliographic_text
from src.utils.reference_matching import normalize_doi

CONSISTENCY_VERSION = "metadata-consistency-v7"  # v6: title differences name the words; v7: shorter author records

VENUE_STOPWORDS = {"the", "of", "and", "on", "in", "for", "&", "a", "an"}

# Brand names that neither abbreviate nor acronym-match their indexed titles.
VENUE_ALIASES = {
    "neurips": "advances in neural information processing systems",
    "nips": "advances in neural information processing systems",
    "icml": "international conference on machine learning",
    "iclr": "international conference on learning representations",
    "aaai": "proceedings of the aaai conference on artificial intelligence",
    "pnas": "proceedings of the national academy of sciences",
    "jama": "journal of the american medical association",
    "bmj": "british medical journal",
}


def _venue_tokens(value: str) -> list[str]:
    text = (value or "").casefold()
    words = re.findall(r"[a-z]+", text)
    if len(words) <= 2:
        for word in list(words):
            if word in VENUE_ALIASES:
                text = text.replace(word, VENUE_ALIASES[word])
        words = re.findall(r"[a-z]+", text)
    return [word for word in words if word not in VENUE_STOPWORDS]


def _ordered_match(short: list[str], full: list[str]) -> bool:
    """Every short token matches, in order, a full token by prefix or an acronym run."""
    position = 0
    for token in short:
        matched = False
        while position < len(full):
            if full[position].startswith(token):
                position += 1
                matched = True
                break
            span = len(token)
            if 2 <= span <= 8 and position + span <= len(full) and \
                    "".join(word[0] for word in full[position:position + span]) == token:
                position += span
                matched = True
                break
            position += 1
        if not matched:
            return False
    return True


def venues_compatible(printed: str | None, source: str | None) -> bool:
    """Tolerate abbreviations and acronyms.

    'J. Mach. Learn. Res.' matches 'Journal of Machine Learning Research', and
    'Proc. IEEE CVPR' matches 'Proceedings of the IEEE Conference on Computer
    Vision and Pattern Recognition'. Missing values are never a difference.
    """
    left, right = _venue_tokens(printed or ""), _venue_tokens(source or "")
    if not left or not right:
        return True
    if left == right:
        return True
    return _ordered_match(left, right) or _ordered_match(right, left)


REPOSITORY_WORDS = ("document server", "repository", "repositorium", "archive", "zenodo", "arxiv",
                    "ssrn", "researchgate", "semantic scholar", "google books", "europe pmc", "pubmed central")


def _repository(venue: str | None) -> bool:
    """Venues that name where a copy is held, not where the work was published."""
    text = (venue or "").casefold()
    return any(word in text for word in REPOSITORY_WORDS)


def _title_words(title: str) -> list[str]:
    """A title's words for comparison: case, punctuation and PDF spacing ("2 -Gbps") ignored."""
    value = re.sub(r"\s*-\s*", "-", unicodedata.normalize("NFKC", str(title or "")).casefold())
    return re.findall(r"[^\W_]+(?:-[^\W_]+)*", value)


def _rejoin(words: list[str], known: set[str]) -> list[str]:
    """Words a PDF split ("equa lizer") or hyphenated differently ("decision feedback"), joined
    the way the other title writes them."""
    result: list[str] = []
    index = 0
    while index < len(words):
        if index + 1 < len(words):
            for joined in (words[index] + words[index + 1], words[index] + "-" + words[index + 1]):
                if joined in known and words[index] not in known:
                    result.append(joined)
                    index += 2
                    break
            else:
                result.append(words[index])
                index += 1
            continue
        result.append(words[index])
        index += 1
    return [word.replace("-", "") if word.replace("-", "") in {k.replace("-", "") for k in known}
            and word not in known else word for word in result]


def title_change(printed: str, source: str) -> str:
    """Which words the two titles do not share: "The database title also has “55-m”."""
    source_words, printed_words = _title_words(source), _title_words(printed)
    printed_words = _rejoin(printed_words, set(source_words))
    source_words = _rejoin(source_words, set(printed_words))
    flat = lambda words: [word.replace("-", "") for word in words]  # noqa: E731
    matcher = SequenceMatcher(None, flat(printed_words), flat(source_words), autojunk=False)
    extra, missing = [], []
    for tag, a1, a2, b1, b2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            extra.append(" ".join(printed_words[a1:a2]))
        if tag in ("replace", "insert"):
            missing.append(" ".join(source_words[b1:b2]))
    parts = []
    if missing:
        parts.append("the database title also has " + ", ".join(f"“{words}”" for words in missing[:3]))
    if extra:
        parts.append("the printed title also has " + ", ".join(f"“{words}”" for words in extra[:3]))
    return "; ".join(parts)


# Words that say what kind of venue it is, or who publishes it, but not which one.
GENERIC_VENUE_WORDS = {"ieee", "acm", "springer", "elsevier", "wiley", "conference", "conferences", "journal",
                       "proceedings", "proc", "international", "intl", "transactions", "trans", "symposium",
                       "workshop", "paper", "papers", "publication", "the", "of", "on", "in", "and"}


VENUE_KINDS = {"conference", "conferences", "journal", "proceedings", "proc", "transactions", "trans",
               "symposium", "workshop"}
# Real venues spelled only with such words.
NAMED_VENUES = {"proceedings of the ieee", "journal of the acm", "proceedings of the acm"}


def generic_venue(venue: str | None) -> bool:
    """"IEEE Conference" or "Journal": a kind of venue, not one that can be looked up. A
    publisher alone ("Springer") is what a book entry should print, so it is not generic."""
    words = re.findall(r"[a-z]+", (venue or "").casefold())
    return (bool(words) and len(words) <= 4 and all(word in GENERIC_VENUE_WORDS for word in words)
            and bool(VENUE_KINDS & set(words)) and " ".join(words) not in NAMED_VENUES)


def names_more(source: str | None, printed: str | None) -> bool:
    """Whether the record's venue names something the printed one does not ("TENCON",
    "Photonics Global") beyond kind, publisher and function words. "Proceedings of the IEEE"
    against "Proc. IEEE", or "Journal of the ACM" against "Journal of ACM", names nothing more:
    those are the journal's own (abbreviated) name, not a generic venue."""
    printed_words = re.findall(r"[a-z]+", (printed or "").casefold())
    extra = [word for word in re.findall(r"[a-z]+", (source or "").casefold())
             if word not in GENERIC_VENUE_WORDS and len(word) > 1
             and not any(word.startswith(short) or short.startswith(word) for short in printed_words)]
    return bool(extra)


def _listed(names: list[str]) -> str:
    return names[0] if len(names) == 1 else "; ".join(names[:-1]) + " and " + names[-1]


def _difference(field, as_cited, candidate, severity, detail):
    return {"field": field, "as_cited": as_cited, "candidate": candidate,
            "severity": severity, "detail": detail}


def compare(parsed: dict, source: dict, *, printed_doi_status: str | None = None) -> list[dict]:
    """All differences between the printed entry and the resolved source."""
    differences = []
    parsed = parsed or {}
    source = source or {}

    printed_doi = normalize_doi(parsed.get("doi")).strip()
    source_doi = normalize_doi(source.get("doi")).strip()
    if printed_doi and source_doi and printed_doi != source_doi and printed_doi_status not in ("unregistered",
                                                                                                "not_indexed"):
        differences.append(_difference("doi", printed_doi, source_doi, "major",
                                       "The printed DOI identifies a different record."))
    if printed_doi and printed_doi_status == "unregistered":
        differences.append(_difference("doi", printed_doi, source_doi, "major",
                                       "doi.org has no record of the printed DOI, so it is mistyped or invalid"
                                       + (f"; the work's DOI is {source_doi}." if source_doi else ".")))
    elif printed_doi and printed_doi_status == "not_indexed":
        differences.append(_difference("doi", printed_doi, source_doi, "minor",
                                       "The printed DOI was not found in the scholarly indexes and its registration "
                                       "could not be confirmed"
                                       + (f"; the matched work's DOI is {source_doi}." if source_doi else ".")))

    printed_title = bibliographic_text(parsed.get("title"))
    source_title = bibliographic_text(source.get("title"))
    chapter_doi = bool(printed_doi and printed_doi == source_doi and printed_title and source_title
                       and printed_title not in source_title and source_title not in printed_title
                       and SequenceMatcher(None, printed_title, source_title).ratio() < .8
                       and bibliographic_text(source.get("venue"))
                       and (printed_title in bibliographic_text(source.get("venue"))
                            or bibliographic_text(source.get("venue")) in printed_title))
    if chapter_doi:
        differences.append(_difference(
            "doi", printed_doi, source_doi, "minor",
            f"The printed DOI identifies one chapter (“{source.get('title')}”) of the cited book, "
            "not the book as a whole."))
    elif printed_title and source_title and printed_title != source_title:
        ratio = SequenceMatcher(None, printed_title, source_title).ratio()
        contains = printed_title in source_title or source_title in printed_title
        change = title_change(parsed.get("title") or "", source.get("title") or "")
        if contains:
            severity, detail = "minor", "One title contains the other (a subtitle was added or dropped)."
        elif ratio >= 0.9:
            severity, detail = "minor", f"Small differences (similarity {ratio:.2f})."
        else:
            severity, detail = "major", f"The titles differ substantially (similarity {ratio:.2f})."
        if change:
            detail = f"{detail[:-1]}: {change}."
        differences.append(_difference("title", parsed.get("title"), source.get("title"), severity, detail))

    printed_year, source_year = parsed.get("year"), source.get("year")
    try:
        gap = abs(int(printed_year) - int(source_year)) if printed_year and source_year else 0
    except (TypeError, ValueError):
        gap = 0
    if gap:
        differences.append(_difference(
            "year", printed_year, source_year, "minor" if gap == 1 else "major",
            "Off by one year — often online-first versus print." if gap == 1
            else f"The years differ by {gap}."))

    # Names are compared by every surname they may have, so "Knorr Cetina, K." and
    # "Karin Knorr Cetina", or a PDF's "Wol ter, A." and the index's "Wolter, A.", agree.
    printed_names = [name for name in parsed.get("authors") or [] if person_key(name)]
    source_names = [author for author in source.get("authors") or [] if person_key(author)]
    if printed_names and source_names:
        if not same_surname(printed_names[0], source_names[0]):
            first = source_names[0]
            differences.append(_difference(
                "first_author", printed_names[0],
                first.get("display_name") if isinstance(first, dict) else first,
                "major", "The first author differs."))
        on_record = set().union(*(surname_variants(author) for author in source_names))
        missing = [(name.get("display_name") or name.get("name") or "" if isinstance(name, dict) else str(name)).strip()
                   for name in printed_names if not surname_variants(name) & on_record]
        printed = set().union(*(surname_variants(name) for name in printed_names))
        # A record of one author proves little about the others, and a different first author
        # is its own (major) difference: the note is only for a longer record, same first author.
        if missing and 2 <= len(source_names) < len(printed_names) \
                and same_surname(printed_names[0], source_names[0]) and all(
                surname_variants(author) & printed for author in source_names):
            # Every author on the record is printed, and the record is shorter: the database
            # dropped names (common for conference papers), not the entry invented them. In a
            # real audit this flagged two genuine co-authors as a difference.
            differences.append(_difference(
                "authors", "; ".join(missing), None, "info",
                f"The database record lists {len(source_names)} of the {len(printed_names)} printed authors; it "
                f"leaves out {_listed(missing).rstrip('.')}. Every author it does list is in the printed entry, so "
                "the record is most likely incomplete; no correction is suggested."))
        elif missing:
            differences.append(_difference(
                "authors", "; ".join(missing), None, "minor",
                f"Printed but not on the source record: {'; '.join(missing)} (the database may list fewer authors)"))

    if not _repository(source.get("venue")) and generic_venue(parsed.get("venue")) \
            and names_more(source.get("venue"), parsed.get("venue")):
        differences.append(_difference("venue", parsed.get("venue"), source.get("venue"), "minor",
                                       "The printed venue is generic: it names no particular conference or "
                                       "journal, and the record does."))
    elif (not _repository(source.get("venue"))
            and not venues_compatible(parsed.get("venue"), source.get("venue"))):
        differences.append(_difference("venue", parsed.get("venue"), source.get("venue"), "minor",
                                       "The printed venue does not match the source record."))
    return differences


def assess_consistency(reference: dict) -> dict:
    """Consistency record for one scored reference."""
    resolution = reference.get("resolution") or {}
    parsed_doi = normalize_doi(((reference.get("original_data") or {}).get("parsed") or {}).get("doi")).strip()
    if resolution.get("status") != "resolved":
        if parsed_doi and resolution.get("printed_doi_status") == "unregistered":
            # doi.org confirms the DOI does not exist and no matching work was found:
            # the signature of a mistyped reference — or of one that does not exist.
            return {"method_version": CONSISTENCY_VERSION, "status": "unresolved_unregistered_doi",
                    "severity": "major",
                    "differences": [_difference(
                        "doi", parsed_doi, None, "major",
                        "doi.org has no record of the printed DOI and no matching work was found in the "
                        "scholarly indexes. Check whether this reference exists as cited.")]}
        if parsed_doi and resolution.get("printed_doi_status") == "not_indexed":
            return {"method_version": CONSISTENCY_VERSION, "status": "unresolved_unindexed_doi",
                    "severity": "minor",
                    "differences": [_difference(
                        "doi", parsed_doi, None, "minor",
                        "Neither the printed DOI nor a matching work was found in the scholarly indexes, and "
                        "doi.org could not be asked. Look the reference up to confirm its details.")]}
        return {"method_version": CONSISTENCY_VERSION, "status": "not_applicable",
                "differences": [], "reason": "Source identity is not resolved."}
    parsed = (reference.get("original_data") or {}).get("parsed") or {}
    differences = compare(parsed, reference.get("external_metadata") or {},
                          printed_doi_status=resolution.get("printed_doi_status"))
    # "info" differences describe the database record, not the entry, and do not rank it.
    severity = ("major" if any(item["severity"] == "major" for item in differences)
                else "minor" if any(item["severity"] == "minor" for item in differences) else "none")
    return {"method_version": CONSISTENCY_VERSION, "status": "compared",
            "severity": severity, "differences": differences}


__all__ = ["CONSISTENCY_VERSION", "assess_consistency", "compare", "venues_compatible"]
