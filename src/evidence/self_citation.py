"""Self-citation identification and pattern analysis.

Self-citation is ordinary scholarship: researchers build on their own work.
What CitePrism V2.0 asks for is the detection of *questionable patterns* — the
cases COPE guidance and journal policies actually care about:

* a self-citation that does not support the claim it is attached to, or is
  never cited in the text at all (citation padding);
* a manuscript whose bibliography is unusually dominated by its own authors;
* self-citations concentrated on one author.

Identity matters more than arithmetic here. Matching on surnames alone merges
every "Wang" and "Müller" into one person, which would wrongly accuse authors
with common names. Matches are therefore tiered, and only the stronger tiers
count toward a pattern:

``orcid``         The same ORCID iD — the person is the same. Strong.
``name_initial``  Same surname *and* first initial — likely the same person.
``surname``       Surname only — recorded for transparency, never counted.

Manuscript ORCID iDs come from operator input and from the manuscript's own
front matter. Every iD is checksum-validated (ISO 7064 MOD 11-2) before use.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

SELF_CITATION_VERSION = "self-citation-v3"

ORCID_PATTERN = re.compile(r"(?<![\dX-])(\d{4}-\d{4}-\d{4}-\d{3}[\dX])(?![\dX-])", re.IGNORECASE)
COUNTED_TIERS = ("orcid", "name_initial")
NAME_PARTICLES = {"van", "von", "der", "den", "de", "del", "della", "da", "di", "du", "le", "la", "bin", "al"}


# ----------------------------------------------------------------------
# ORCID handling
# ----------------------------------------------------------------------
def orcid_checksum_ok(identifier: str) -> bool:
    digits = identifier.replace("-", "").upper()
    if not re.fullmatch(r"\d{15}[\dX]", digits):
        return False
    total = 0
    for character in digits[:-1]:
        total = (total + int(character)) * 2
    result = (12 - total % 11) % 11
    expected = "X" if result == 10 else str(result)
    return digits[-1] == expected


def normalize_orcid(value) -> str | None:
    """Return the bare 0000-0000-0000-000X form of a valid iD, else None."""
    if not value:
        return None
    match = ORCID_PATTERN.search(str(value))
    if not match:
        return None
    identifier = match.group(1).upper()
    return identifier if orcid_checksum_ok(identifier) else None


def orcids_in_text(text: str) -> list[str]:
    found = []
    for match in ORCID_PATTERN.finditer(text or ""):
        identifier = normalize_orcid(match.group(1))
        if identifier and identifier not in found:
            found.append(identifier)
    return found


def front_matter_orcids(source_document: dict | None, pages: int = 2) -> list[str]:
    """ORCID iDs printed on the first pages — the author block, not the bibliography."""
    if not source_document:
        return []
    text = "\n".join(page.get("text") or "" for page in (source_document.get("pages") or [])[:pages])
    return orcids_in_text(text)


# ----------------------------------------------------------------------
# Name handling
# ----------------------------------------------------------------------
APOSTROPHES = re.compile(r"[\u2019\u2018\u02bc\u0060\u00b4\u2032]")


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(character for character in decomposed if not unicodedata.combining(character)).casefold()


LETTERS = r"[^\W\d_][^\W\d_'\-]*(?:['\-][^\W\d_]+)*"
TITLES = re.compile(r"^(?:(?:prof|dr|pd|priv\.?-?doz|mr|mrs|ms|mx|dipl\.?-?(?:ing|kfm|psych)?|mag|ing)\.?\s+)+",
                    re.IGNORECASE)
# Degrees are removed only where they cannot be a name. "Yi Ma", "Jean Mba" and
# "Rahman, Md" (Md = Muhammad) are names; "John Smith PhD", "Anna Meier M.A.",
# "Smith, John, MD" and "John Smith, MA" carry degrees.
DEGREE = r"(?:ph\.?\s?d|m\.?d|m\.?sc|b\.?sc|m\.?a|m\.?b\.?a|ll\.?m)"
DOTTED_DEGREE = re.compile(r"\s+(?:ph\.\s?d|m\.d|m\.sc|b\.sc|m\.a|m\.b\.a|ll\.m)\.?\s*$", re.IGNORECASE)
CASED_DEGREE = re.compile(r"\s+(?:PhD|PHD|MSc|BSc|MBA|LLM|DPhil)\.?\s*$")     # case-sensitive
COMMA_DEGREE = re.compile(rf",\s*{DEGREE}\.?\s*$", re.IGNORECASE)


SPELLED_DEGREE = re.compile(r"(?:ph\.?\s?d|m\.?sc|b\.?sc|m\.?b\.?a|ll\.?m|dphil)\.?$", re.IGNORECASE)


def _strip_degree(text: str) -> str:
    match = COMMA_DEGREE.search(text)
    if match:
        head, degree = text[:match.start()], text[match.start():].lstrip(", ").strip()
        if SPELLED_DEGREE.fullmatch(degree):
            return head                      # "Smith, John, Ph.D.", "John Smith, MSc"
        # Two letters may be initials: "Garcia Lopez, M.A." is Miguel Angel Garcia Lopez.
        # Only the undotted capitals after a full name are a degree: "Jane Doe, MD".
        if "." not in degree and degree.isupper() and ("," in head or len(head.split()) >= 2):
            return head
        return text
    match = CASED_DEGREE.search(text) if not text.isupper() else None
    if match and re.findall(r"[^\W\d_]{2,}", text[:match.start()]):
        return text[:match.start()]          # "Chen MSc", "John Smith PhD": never initials
    match = DOTTED_DEGREE.search(text)
    if match and len(re.findall(r"[^\W\d_]{2,}", text[:match.start()])) >= 2:
        return text[:match.start()]          # "Anna Meier M.A." — but "Hopkins M.D." is a surname and initials
    return text


def _strip_degrees(text: str) -> str:
    """Every trailing degree: "Jane Doe, MD, PhD" -> "Jane Doe"."""
    for _ in range(3):
        stripped = _strip_degree(text)
        if stripped == text:
            break
        text = stripped
    return text


# "Jane Doe, M.D." or "Yi Ma MD": a name with a degree, or a surname with the initials M. D.
AMBIGUOUS_DEGREE = re.compile(r"^(?P<head>.*?[^\W\d_].*?)\s*,?\s+(?P<degree>M\.?\s?[AD]\.?)\s*$")


SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def clean_name(name) -> str:
    """A printed author name without affiliation marks, titles, degrees, roles or suffixes.

    "Anna Meier1,2", "Dr. Anna Meier", "Anna Meier (corresponding author)",
    "John Smith Jr." and "Smith, John, Jr." are all just the person's name.
    """
    text = unicodedata.normalize("NFKC", str(name or ""))
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text)                       # (corresponding author), [1]
    text = re.sub(r"(?<=[^\W\d_])[\d*†‡§¶#]+(?:\s*,\s*[\d*†‡§¶#]+)*", " ", text)  # affiliation marks: Meier1,2
    text = re.sub(r"[*†‡§¶#]", " ", text)
    text = TITLES.sub("", text.strip())
    stripped = _strip_degrees(text)
    if re.findall(r"[^\W\d_]{2,}", stripped):
        text = stripped                      # never strip the only name left
    words = [word for word in re.split(r"(\s+|,)", text)]
    kept = [word for word in words if word.strip(" .").lower() not in SUFFIXES]
    return re.sub(r"\s+", " ", "".join(kept)).strip(" ,;")


def person_key(name) -> tuple[str, str] | None:
    """(surname, first initial) from the name forms bibliographies and indexes use.

    Handles "Meier, Anna", "Anna Meier", "A. Meier", "Meier A", "Meier AB" and
    "Smith JA" (Vancouver), in any script, after removing affiliation marks,
    titles and suffixes. This is the one name parser for identity resolution,
    metadata comparison and self-citation, so they always agree on who is who.
    Returns None when no surname can be identified.
    """
    if isinstance(name, dict):
        name = name.get("display_name") or name.get("name") or ""
    cleaned = APOSTROPHES.sub("'", clean_name(name))
    text = _fold(cleaned).strip()
    if not text or text in {"et al", "et al."}:
        return None
    if "," in text:
        surname_part, given_part = text.split(",", 1)
        surname_words = re.findall(LETTERS, surname_part)
        given_words = re.findall(r"[^\W\d_]+", given_part)
    else:
        words = re.findall(LETTERS, text)
        if not words:
            return None
        original = cleaned.split()
        initials = re.compile(r"(?:[A-ZÀ-ÞĀ-Ž]\.?){1,3}")
        trailing_initials = (
            len(original) >= 2
            and initials.fullmatch(original[-1]) is not None
            and initials.fullmatch(original[0]) is None
        )
        # "Meier A" / "Meier AB" / "Smith JA" / "Ng A": surname first, then initials.
        leading = 0
        while leading < len(original) - 1 and initials.fullmatch(original[leading]):
            leading += 1
        if trailing_initials:
            surname_words = re.findall(LETTERS, _fold(" ".join(original[:-1])))
            given_words = [_fold(original[-1])]
        elif leading and len(original) - leading >= 2:
            # "K. Knorr Cetina", "M.F.N. Omran Zailuddin", "P. Fel ten": after the
            # initials, everything is the surname (indexes print names this way).
            surname_words = re.findall(LETTERS, _fold(" ".join(original[leading:])))
            given_words = [_fold(original[0])]
        else:
            surname_words, given_words = [words[-1]], words[:-1]
            # Keep particles attached: "Ludwig van Beethoven" -> "vanbeethoven".
            while given_words and given_words[-1] in NAME_PARTICLES:
                surname_words.insert(0, given_words.pop())
    surname = "".join(re.sub(r"[\W\d_]", "", word) for word in surname_words)
    if len(surname) < 2:
        return None
    initial = given_words[0][0] if given_words and given_words[0] else ""
    return surname, initial


def _single_variants(name) -> set[str]:
    """Every surname a name may have, compacted ("knorrcetina", "cetina").

    "Karin Knorr Cetina" does not say where the given names end, so both
    readings are kept; "Knorr Cetina, K." and "K. Knorr Cetina" are
    unambiguous. Two names can be the same person only when these sets meet.
    """
    key = person_key(name)
    if not key:
        return set()
    variants = {key[0]}
    if isinstance(name, dict):
        name = name.get("display_name") or name.get("name") or ""
    cleaned = APOSTROPHES.sub("'", clean_name(name))
    if "," not in cleaned:
        words = [re.sub(r"[\W\d_]", "", _fold(word)) for word in cleaned.split()]
        words = [word for word in words if word]
        last = cleaned.split()[-1] if cleaned.split() else ""
        if len(words) >= 2 and len(words[-1]) >= 2 and not re.fullmatch(r"(?:[A-ZÀ-ÞĀ-Ž]\.?){1,3}", last):
            variants.add(words[-1])          # "T. Hui Teo", "J. Michael Smith": the last name alone too
        for count in (2, 3):
            if len(words) > count:
                variants.add("".join(words[-count:]))
    return {variant for variant in variants if len(variant) >= 2}


# Common Spanish and Portuguese surnames (not also common given names): "Garcia Lopez, M.A."
# is a double surname and initials, never "Garcia Lopez" with a master's degree.
IBERIAN_SURNAMES = {
    "garcia", "lopez", "martinez", "rodriguez", "gonzalez", "hernandez", "perez", "sanchez", "ramirez", "torres",
    "flores", "rivera", "gomez", "diaz", "ruiz", "alvarez", "jimenez", "moreno", "munoz", "romero", "alonso",
    "gutierrez", "navarro", "dominguez", "vazquez", "ramos", "serrano", "blanco", "molina", "morales",
    "suarez", "ortega", "delgado", "castro", "ortiz", "rubio", "sanz", "iglesias", "nunez", "medina",
    "garrido", "cortes", "castillo", "lozano", "guerrero", "cano", "prieto", "mendez", "calvo",
    "gallego", "herrera", "marquez", "pena", "cabrera", "campos", "vega", "fuentes", "carrasco",
    "diez", "caballero", "nieto", "aguilar", "santana", "herrero", "montero",
    "hidalgo", "gimenez", "ibanez", "ferrer", "duran", "benitez", "vargas", "arias",
    "carmona", "crespo", "pastor", "soto", "saez", "velasco", "moya", "soler", "parra",
    "bravo", "gallardo", "rojas", "ferreira", "pereira", "oliveira", "costa", "rodrigues", "martins",
    "fernandes", "gomes", "lopes", "sousa", "souza", "carvalho", "almeida", "ribeiro", "pinto", "teixeira",
    "moreira", "correia", "nunes", "soares", "vieira", "monteiro", "cardoso", "rocha", "neves",
    "coelho", "cunha", "pires", "marques", "araujo", "barbosa", "fernandez", "mendoza",
    "chavez", "guzman", "salazar", "contreras", "figueroa", "espinoza", "sandoval", "castaneda", "valencia",
}


def readings(name, *, degree: bool = True) -> list[tuple[set[str], str]]:
    """Every way to read a name: (possible surnames, first initial).

    Usually one. "Jane Doe, M.D." has two — Jane Doe with a degree, or a surname
    "Jane Doe" with initials M. D. — and both are kept rather than guessed, unless
    `degree` is False (bibliography names, where "Garcia Lopez MD" is a surname and
    initials) or the name opens with a surname ("Garcia Lopez, M.A.").
    """
    if isinstance(name, dict):
        name = name.get("display_name") or name.get("name") or ""
    found = []
    candidates = [name]
    match = AMBIGUOUS_DEGREE.match(clean_name(str(name or ""))) if degree else None
    if match:
        head = match.group("head").replace(",", " ").split()
        # "Garcia Lopez, M.A." (two surnames) is a surname with initials; "Leon Smith, M.D."
        # may be Leon Smith with a degree.
        if len(head) >= 2 and not all(_fold(word).strip(".") in IBERIAN_SURNAMES for word in head):
            candidates.append(match.group("head"))
    for candidate in candidates:
        key = person_key(candidate)
        if key:
            found.append((_single_variants(candidate), key[1]))
    return found


def surname_variants(name) -> set[str]:
    """Every surname a name may have, compacted ("knorrcetina", "cetina")."""
    found = readings(name)
    return set().union(*(variants for variants, _ in found)) if found else set()


def same_surname(left, right) -> bool:
    return bool(surname_variants(left) & surname_variants(right))


def author_records(authors, *, degree_readings: bool = False) -> list[dict]:
    """Normalize reference authors (strings or provider dicts) with ORCIDs.

    A name that may carry a degree ("Jane Doe, M.D.") is read both ways only with
    `degree_readings` — for the manuscript's own authors. In a bibliography, "Garcia
    Lopez MD" is a surname with initials, and reading it as "Garcia Lopez" with a degree
    would make every "Gonzalo Lopez" a self-citation.
    """
    records = []
    for author in authors or []:
        name = author.get("display_name") or author.get("name") if isinstance(author, dict) else author
        key = person_key(name)
        if not key:
            continue
        orcid = normalize_orcid(author.get("orcid")) if isinstance(author, dict) else None
        found = readings(name, degree=degree_readings)
        records.append({"name": str(name), "key": key, "orcid": orcid,
                        "variants": set().union(*(variants for variants, _ in found)) if found else set(),
                        "readings": found})
    return records


def match_person(manuscript_author: dict, reference_author: dict) -> str | None:
    if manuscript_author.get("orcid") and reference_author.get("orcid"):
        # Two known, different iDs are two different people, whatever the names say.
        return "orcid" if manuscript_author["orcid"] == reference_author["orcid"] else None
    mine = manuscript_author.get("readings") or [(manuscript_author.get("variants") or {manuscript_author["key"][0]},
                                                   manuscript_author["key"][1])]
    theirs = reference_author.get("readings") or [(reference_author.get("variants") or {reference_author["key"][0]},
                                                   reference_author["key"][1])]
    best = None
    for surnames, initial in mine:
        for other_surnames, other_initial in theirs:
            if not surnames & other_surnames:
                continue
            if initial and other_initial:
                if initial == other_initial:
                    return "name_initial"
                continue                     # same surname, different initial: another person
            best = "surname"
    return best


def manuscript_people(authors, orcids: list[str]) -> list[dict]:
    """Manuscript authors, with ORCIDs attached when the count lines up.

    ORCIDs are attached positionally only when exactly as many valid iDs as
    authors are known; otherwise they are kept as an unattributed set, which
    still matches by iD but never mislabels which author holds which iD.
    """
    people = author_records(authors, degree_readings=True)
    if orcids and len(orcids) == len(people):
        for person, identifier in zip(people, orcids):
            person["orcid"] = identifier
    return people


def attach_orcids(people: list[dict], orcids: list[str], references: list[dict]) -> list[dict]:
    """Attach iDs to manuscript authors only where that is unambiguous.

    One author and one iD: they belong together. Otherwise an iD is attached when
    a reference author holding it has the name of exactly one manuscript author.
    Never by list position — an iD list typed in another order would otherwise
    name the wrong person in a self-citation finding.
    """
    if not orcids:
        return people
    if len(people) == 1 and len(orcids) == 1:
        people[0]["orcid"] = orcids[0]
        return people
    taken = set()
    for reference in references:
        for author in reference_authors(reference):
            identifier = author.get("orcid")
            if not identifier or identifier not in orcids or identifier in taken:
                continue
            named = [person for person in people if not person.get("orcid")
                     and match_person({**person, "orcid": None}, {**author, "orcid": None}) in COUNTED_TIERS]
            if len(named) == 1:
                named[0]["orcid"] = identifier
                taken.add(identifier)
    return people


# ----------------------------------------------------------------------
# Analysis
# ----------------------------------------------------------------------
def reference_authors(reference: dict) -> list[dict]:
    """Prefer the resolved source's author list; fall back to the printed one."""
    external = (reference.get("external_metadata") or {}).get("authors") or []
    printed = ((reference.get("original_data") or {}).get("parsed") or {}).get("authors") or []
    records = author_records(external)
    return records if records else author_records(printed)


def classify_reference(people: list[dict], unattributed: set[str], reference: dict) -> dict:
    authors = reference_authors(reference)
    matches = []
    for author in authors:
        if author.get("orcid") and author["orcid"] in unattributed:
            # Name the manuscript author only when the names agree too.
            named = next((person["name"] for person in people if match_person(person, author) in COUNTED_TIERS), None)
            matches.append({"reference_author": author["name"], "manuscript_author": named,
                            "tier": "orcid", "orcid": author["orcid"]})
            continue
        for person in people:
            tier = match_person(person, author)
            if tier:
                matches.append({"reference_author": author["name"], "manuscript_author": person["name"],
                                "tier": tier, "orcid": author.get("orcid") if tier == "orcid" else None})
                break
    tiers = {match["tier"] for match in matches}
    strongest = next((tier for tier in ("orcid", "name_initial", "surname") if tier in tiers), None)
    return {
        "is_self_citation": strongest in COUNTED_TIERS,
        "tier": strongest or "none",
        "matches": matches,
        "reference_author_count": len(authors),
    }


def analyze_self_citations(
    manuscript_authors,
    references: list[dict],
    *,
    supplied_orcids: list[str] | None = None,
    front_matter: list[str] | None = None,
    rate_threshold: float = 0.25,
    minimum_count: int = 3,
    concentration_threshold: float = 0.15,
) -> dict:
    """Classify every reference, then look for questionable patterns."""
    supplied = [identifier for identifier in (normalize_orcid(v) for v in supplied_orcids or []) if identifier]
    printed = [identifier for identifier in (front_matter or []) if identifier not in supplied]
    orcids = supplied + printed
    # iDs are never attached to authors by list position: a list typed in another
    # order would name the wrong person. An iD matches the reference author who
    # holds it, and that author is named as the reference record names them.
    people = attach_orcids(manuscript_people(manuscript_authors, []), orcids, references)
    attributed = {person["orcid"] for person in people if person.get("orcid")}
    unattributed = set(orcids) - attributed

    per_reference: dict[str, dict] = {}
    by_author: Counter = Counter()
    questionable: list[dict] = []
    for reference in references:
        uid = reference.get("reference_uid") or str(reference.get("ref_id"))
        result = classify_reference(people, unattributed, reference)
        per_reference[uid] = result
        if not result["is_self_citation"]:
            continue
        for match in result["matches"]:
            if match["tier"] in COUNTED_TIERS:
                by_author[match["manuscript_author"]
                          or f"{match['reference_author']} (ORCID {match['orcid']})"] += 1
        reasons = []
        relevance = reference.get("relevance") or {}
        if reference.get("citation_count", 0) == 0:
            reasons.append("not_cited_in_text")
        if relevance.get("label") == "potentially_irrelevant" or relevance.get("unrelated_uses"):
            reasons.append("potentially_irrelevant")
        if reasons:
            questionable.append({"reference_id": uid, "marker": str(reference.get("ref_id")),
                                 "reasons": reasons, "tier": result["tier"]})
        result["questionable_reasons"] = reasons

    total = len(references)
    counted = sum(result["is_self_citation"] for result in per_reference.values())
    rate = counted / total if total else 0.0
    patterns = []
    if counted >= minimum_count and rate >= rate_threshold:
        patterns.append({
            "code": "high_self_citation_rate",
            "detail": f"{counted} of {total} references ({rate:.0%}) share an author with the manuscript; "
                      f"the configured review threshold is {rate_threshold:.0%}.",
        })
    for author, count in by_author.most_common(3):
        share = count / total if total else 0.0
        if count >= minimum_count and share >= concentration_threshold:
            patterns.append({
                "code": "author_concentration",
                "detail": f"{author} appears on {count} cited references ({share:.0%} of the bibliography).",
            })
    padding = [item for item in questionable if "not_cited_in_text" in item["reasons"]]
    if len(padding) >= 2:
        patterns.append({
            "code": "uncited_self_citations",
            "detail": f"{len(padding)} self-citations are listed in the bibliography but never cited in the text.",
        })
    irrelevant = [item for item in questionable if "potentially_irrelevant" in item["reasons"]]
    if len(irrelevant) >= 2:
        patterns.append({
            "code": "low_relevance_self_citations",
            "detail": f"{len(irrelevant)} self-citations may not support the claims they are attached to.",
        })

    tier_counts = Counter(result["tier"] for result in per_reference.values())
    assessable = bool(people or orcids)
    return {
        "method_version": SELF_CITATION_VERSION,
        # Without author names or iDs nothing can be matched: "no self-citations"
        # would be false reassurance, so the analysis says it could not be done.
        "status": "assessed" if assessable else "not_assessable",
        "reason": None if assessable else (
            "The manuscript's authors could not be read and no ORCID iDs were supplied, so references could not "
            "be compared with the authors."),
        "manuscript_authors": [person["name"] for person in people],
        "orcids": {"supplied": supplied, "front_matter": printed,
                   "attributed": sorted(attributed), "unattributed": sorted(unattributed)},
        "per_reference": per_reference,
        "counts": {
            "total_references": total,
            "self_citations": counted,
            "rate": round(rate, 4),
            "orcid_confirmed": tier_counts.get("orcid", 0),
            "name_initial_matches": tier_counts.get("name_initial", 0),
            "surname_only_matches": tier_counts.get("surname", 0),
        },
        "by_author": dict(by_author),
        "questionable": questionable,
        "patterns": patterns,
        "thresholds": {"rate": rate_threshold, "minimum_count": minimum_count,
                       "author_concentration": concentration_threshold},
        "guidance": (
            "Self-citation is normal and often necessary. These signals ask a reviewer "
            "to check whether specific self-citations are relevant; they are not "
            "findings of misconduct or citation manipulation."
        ),
        "identity_note": (
            "ORCID matches identify the same person. Name-and-initial matches are "
            "probable. Surname-only matches are shown but never counted."
        ),
    }


__all__ = [
    "SELF_CITATION_VERSION",
    "analyze_self_citations",
    "front_matter_orcids",
    "normalize_orcid",
    "orcid_checksum_ok",
    "orcids_in_text",
    "person_key",
    "readings",
    "same_surname",
    "surname_variants",
]
