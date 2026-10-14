"""Error-injection benchmark: known errors planted in a real document.

Human-annotated data is the gold standard for measuring CitePrism, and none is
available yet. This benchmark answers a narrower question that needs no
annotator: *when a known error is planted, is it found — and does anything
else get flagged?* Every planted error is ground truth by construction.

What it measures, per detector:

* **citation year typos** — a year in an in-text citation is changed (2023 ->
  2024); the year-conflict detector should report exactly that citation.
* **dropped citations** — every in-text citation of one entry is blanked out;
  the entry should become "listed but never cited", and nothing else should.
* **metadata differences** — a published record is built from the printed
  entry and then perturbed (year, title, first author, DOI); the consistency
  check should report the changed field with the right severity, and report
  nothing for harmless variants (letter case, name order, a dropped subtitle).
* **identity resolution** — the right record is offered among same-author
  decoys (or left out entirely); the resolver should pick it, or pick nothing.
* **self-citation** — reference authors are written in the forms
  bibliographies use; same person must match, a different initial must not.

What it does not measure: relevance or support judgements (they need a model
and human labels), coverage of the scholarly databases, or how often such
errors occur in real manuscripts. Report the numbers with that scope.
"""

from __future__ import annotations

import copy
import random
import re
from collections import Counter
from typing import Callable, Optional

from src.evidence.consistency import compare
from src.evidence.extraction import anchor_manuscript
from src.evidence.identity import resolve_candidates
from src.evidence.self_citation import classify_reference, manuscript_people
from src.evidence.text import bibliographic_text, canonical_id

YEAR = re.compile(r"(?<!\d)(1[5-9]\d\d|20\d\d)(?!\d)")
BENCHMARK_VERSION = "injection-v1"


def _anchor(source: dict) -> dict:
    return anchor_manuscript({"metadata": {}, "citations_in_text": [], "references_list": []}, source)


def _key(reference: dict) -> str:
    return canonical_id(reference["ref_id"])


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 4) if denominator else None


def _mismatches(result: dict) -> set[tuple[str, str, int]]:
    found = set()
    for reference in result["references_list"]:
        for conflict in reference.get("citation_year_mismatches") or []:
            page = ((conflict.get("source_locations") or [{}])[0] or {}).get("page")
            found.add((_key(reference), str(conflict.get("cited_year"))[:4], page))   # "2025b" -> 2025
    return found


def _years_by_surname(result: dict) -> dict[str, set[str]]:
    """Years listed for each leading name, so a changed year never names another real entry."""
    years: dict[str, set[str]] = {}
    for reference in result["references_list"]:
        label = str(reference.get("ref_id") or "")
        surname = re.split(r"\s+(?=\d|o\.|i\.|n\.d)", label)[0].lower()
        year = YEAR.search(label)
        if year:
            years.setdefault(surname, set()).add(year.group(1))
    return years


# ---------------------------------------------------------------- year typos
def year_typos(source: dict, baseline: dict, *, per_round: int, rounds: int, seed: int) -> dict:
    """Change the year of single-reference in-text citations; count what the detector reports."""
    pages = {page["page"]: page for page in source["pages"]}
    by_key = {_key(reference): reference for reference in baseline["references_list"]}
    years_by_name = _years_by_surname(baseline)
    before = _mismatches(baseline)
    candidates = []
    for occurrence in baseline["citations_in_text"]:
        keys = occurrence.get("reference_keys") or []
        location = (occurrence.get("source_locations") or [None])[0]
        if len(keys) != 1 or not location or keys[0] not in by_key:
            continue
        page = pages.get(location.get("page"))
        if page is None:
            continue
        span = page["text"][location["char_start"]:location["char_end"]]
        years = list(YEAR.finditer(span))
        listed = YEAR.search(str(by_key[keys[0]].get("ref_id") or ""))
        if len(years) != 1 or not listed or years[0].group(1) != listed.group(1):
            continue
        if any(key == keys[0] for key, _, _ in before):
            continue  # already a real conflict: not a clean place to plant one
        candidates.append((keys[0], location["page"], location["char_start"] + years[0].start(), years[0].group(1),
                           str(by_key[keys[0]]["ref_id"])))
    rng = random.Random(seed)
    planted = detected = relinked = false_alarms = 0
    examples_missed: list[str] = []
    planted_before: set[tuple] = set()            # each citation is planted once: trials are distinct
    for _ in range(rounds):
        chosen, used = [], set()
        for candidate in rng.sample(candidates, len(candidates)):
            if candidate[0] in used or candidate[:3] in planted_before:
                continue
            surname = re.split(r"\s+(?=\d|o\.|i\.|n\.d)", candidate[4])[0].lower()
            year = int(candidate[3])
            others = [int(value) for value in years_by_name.get(surname, set()) if value != candidate[3]]
            # Only unambiguous typos: no other work by that author is as close to the new year.
            new = next((str(value) for value in (year + 1, year - 1, year + 2)
                        if all(abs(value - other) > abs(value - year) for other in others)), None)
            if new is None:
                continue
            chosen.append((*candidate, new))
            used.add(candidate[0])
            planted_before.add(candidate[:3])
            if len(chosen) == per_round:
                break
        if not chosen:
            break
        mutated = copy.deepcopy(source)
        mutated_pages = {page["page"]: page for page in mutated["pages"]}
        for key, page, offset, old, _, new in chosen:
            text = mutated_pages[page]["text"]
            assert text[offset:offset + 4] == old
            mutated_pages[page]["text"] = text[:offset] + new + text[offset + 4:]
        result = _anchor(mutated)
        after = _mismatches(result)
        expected = {(key, new, page) for key, page, _, _, _, new in chosen}
        for item in expected:
            planted += 1
            if item in after:
                detected += 1
            else:
                key, new, page = item
                linked_elsewhere = any(
                    (o.get("source_locations") or [{}])[0].get("page") == page and key not in (o.get("reference_keys") or [])
                    and new in str(o.get("marker")) for o in result["citations_in_text"])
                relinked += linked_elsewhere
                if len(examples_missed) < 5:
                    examples_missed.append(f"{key} cited as {new} on p. {page}")
        false_alarms += len(after - before - expected)
    return {"planted": planted, "detected": detected, "recall": _ratio(detected, planted),
            "linked_to_another_entry": relinked, "unexpected_new_conflicts": false_alarms,
            "precision": _ratio(detected, detected + false_alarms), "eligible_citations": len(candidates),
            "missed_examples": examples_missed}


# ---------------------------------------------------------- dropped citations
def _cited(result: dict) -> set[str]:
    return {key for occurrence in result["citations_in_text"] for key in occurrence.get("reference_keys") or []}


def dropped_citations(source: dict, baseline: dict, *, trials: int, seed: int) -> dict:
    """Blank out every in-text citation of one entry; it should be the only entry that becomes uncited."""
    pages = {page["page"]: page for page in source["pages"]}
    cited_before = _cited(baseline)
    occurrences_by_key: dict[str, list[dict]] = {}
    for occurrence in baseline["citations_in_text"]:
        keys = occurrence.get("reference_keys") or []
        for key in keys:
            occurrences_by_key.setdefault(key, []).append(occurrence)
    # Only entries whose every citation names that entry alone, so blanking removes nothing else.
    eligible = [key for key, items in occurrences_by_key.items()
                if all(len(item.get("reference_keys") or []) == 1 and item.get("source_locations") for item in items)
                and not any(item.get("footnote") for item in items)]
    rng = random.Random(seed)
    chosen = rng.sample(eligible, min(trials, len(eligible)))
    detected = collateral = 0
    for key in chosen:
        mutated = copy.deepcopy(source)
        mutated_pages = {page["page"]: page for page in mutated["pages"]}
        for occurrence in occurrences_by_key[key]:
            location = occurrence["source_locations"][0]
            page = mutated_pages[location["page"]]
            start, end = location["char_start"], location["char_end"]
            page["text"] = page["text"][:start] + " " * (end - start) + page["text"][end:]
        assert pages  # the original is untouched
        cited_after = _cited(_anchor(mutated))
        detected += key not in cited_after
        collateral += len((cited_before - cited_after) - {key})
    return {"trials": len(chosen), "detected": detected, "recall": _ratio(detected, len(chosen)),
            "other_entries_lost": collateral, "eligible_entries": len(eligible)}


# ------------------------------------------------------- metadata differences
def _record(parsed: dict) -> dict:
    return {"title": parsed.get("title"), "year": parsed.get("year"), "doi": parsed.get("doi"),
            "venue": parsed.get("venue"),
            "authors": [{"display_name": name} for name in parsed.get("authors") or []]}


def _swap_letters(title: str, rng: random.Random) -> Optional[str]:
    words = [match for match in re.finditer(r"[^\W\d_]{6,}", title)]
    if not words:
        return None
    # Two different letters, so the swap really changes the title ("ll" swapped is no typo).
    spots = [index for word in words for index in range(word.start() + 1, word.end() - 2)
             if title[index] != title[index + 1]]
    if not spots:
        return None
    index = rng.choice(spots)
    return title[:index] + title[index + 1] + title[index] + title[index + 2:]


def _has_subtitle(title: str) -> bool:
    from src.evidence.identity import _main_title
    return _main_title(title) != bibliographic_text(title)


def _given_first(name: str) -> str:
    if "," not in name:
        return name
    surname, given = [part.strip() for part in name.split(",", 1)]
    return f"{given} {surname}".strip()


PERTURBATIONS: dict[str, tuple[Optional[str], Optional[str]]] = {
    # name: (field expected to differ, expected severity); None = no difference expected
    "unchanged": (None, None),
    "harmless_case_and_name_order": (None, None),
    "subtitle_added_by_database": ("title", "minor"),
    "year_off_by_one": ("year", "minor"),
    "year_wrong": ("year", "major"),
    "title_typo": ("title", "minor"),
    "title_of_another_work": ("title", "major"),
    "first_author_replaced": ("first_author", "major"),
    "doi_mistyped": ("doi", "major"),
}


def metadata_differences(baseline: dict, *, seed: int) -> dict:
    rng = random.Random(seed)
    entries = [r["parsed"] for r in baseline["references_list"]
               if (r.get("parsed") or {}).get("title") and len(r["parsed"]["title"]) >= 20
               and (r.get("parsed") or {}).get("authors") and (r.get("parsed") or {}).get("year")]
    results: dict[str, Counter] = {name: Counter() for name in PERTURBATIONS}
    for parsed in entries:
        others = [other for other in entries if other is not parsed]
        for name, (field, severity) in PERTURBATIONS.items():
            record = _record(parsed)
            if name == "harmless_case_and_name_order":
                record["title"] = str(parsed["title"]).upper()
                record["authors"] = [{"display_name": _given_first(author)} for author in parsed["authors"]]
            elif name == "subtitle_added_by_database":
                if _has_subtitle(parsed["title"]):
                    continue          # a database does not add a second subtitle
                record["title"] = f"{parsed['title']}: an extended subtitle from the publisher"
            elif name == "year_off_by_one":
                record["year"] = int(parsed["year"]) + 1
            elif name == "year_wrong":
                record["year"] = int(parsed["year"]) + rng.choice((-5, -3, 3, 4))
            elif name == "title_typo":
                record["title"] = _swap_letters(str(parsed["title"]), rng)
                if not record["title"]:
                    continue
            elif name == "title_of_another_work":
                # Another work, not the same work listed again in another chapter.
                different = [o for o in others if bibliographic_text(o["title"]) != bibliographic_text(parsed["title"])]
                if not different:
                    continue
                record["title"] = rng.choice(different)["title"]
            elif name == "first_author_replaced":
                pool = [o["authors"][0] for o in others
                        if o["authors"][0].split(",")[0].lower() != parsed["authors"][0].split(",")[0].lower()]
                if not pool:
                    continue
                record["authors"] = [{"display_name": rng.choice(pool)}] + record["authors"][1:]
            elif name == "doi_mistyped":
                if not parsed.get("doi"):
                    continue
                doi = str(parsed["doi"])
                last = doi[-1]
                record["doi"] = doi[:-1] + (str((int(last) + 1) % 10) if last.isdigit() else ("x" if last != "x" else "y"))
            differences = compare(parsed, record, printed_doi_status="registered")
            fields = {d["field"] for d in differences}
            results[name]["trials"] += 1
            if field is None:
                results[name]["correct"] += not differences
                results[name]["false_alarm"] += bool(differences)
            else:
                hit = next((d for d in differences if d["field"] == field), None)
                results[name]["detected"] += hit is not None
                results[name]["right_severity"] += bool(hit and hit["severity"] == severity)
                # A replaced first author is, correctly, also an author missing from the record.
                expected_fields = {field, "authors"} if field == "first_author" else {field}
                results[name]["other_fields_flagged"] += bool(fields - expected_fields)
    summary = {}
    for name, counts in results.items():
        trials = counts["trials"]
        if PERTURBATIONS[name][0] is None:
            summary[name] = {"trials": trials, "no_difference_reported": _ratio(counts["correct"], trials)}
        else:
            summary[name] = {"trials": trials, "detected": _ratio(counts["detected"], trials),
                             "right_severity": _ratio(counts["right_severity"], trials),
                             "other_fields_also_flagged": _ratio(counts["other_fields_flagged"], trials)}
    return {"entries": len(entries), "perturbations": summary}


# ------------------------------------------------------- identity resolution
def _work(parsed: dict, **changes) -> dict:
    work = {"id": changes.pop("id", None), "title": parsed.get("title"), "doi": parsed.get("doi"),
            "publication_year": parsed.get("year"),
            "authors": [{"display_name": name} for name in parsed.get("authors") or []]}
    work.update(changes)
    return work


def identity_resolution(baseline: dict, *, seed: int, decoys: int = 5) -> dict:
    """The right record among same-author or same-topic decoys; or absent, when nothing must be picked."""
    rng = random.Random(seed)
    entries = [r["parsed"] for r in baseline["references_list"]
               if (r.get("parsed") or {}).get("title") and len(r["parsed"]["title"]) >= 20
               and (r.get("parsed") or {}).get("authors")]
    variants: dict[str, Callable[[dict], dict]] = {
        "exact_record": lambda p: _work(p, id="right"),
        "database_adds_subtitle": lambda p: None if _has_subtitle(p["title"]) else
        _work(p, id="right", title=f"{p['title']}: a subtitle the entry omits"),
        "title_with_typo": lambda p: _work(p, id="right", title=_swap_letters(str(p["title"]), rng) or p["title"]),
        "names_given_first": lambda p: _work(p, id="right", authors=[{"display_name": _given_first(a)}
                                                                      for a in p["authors"]]),
    }
    outcomes = {name: Counter() for name in [*variants, "right_record_absent"]}

    def same_work(left: dict, right: dict) -> bool:
        # An edited volume lists one work in several chapters; such a copy is not a decoy.
        return bibliographic_text(left.get("title")) == bibliographic_text(right.get("title"))

    for parsed in entries:
        surname = parsed["authors"][0].split(",")[0].strip().lower()
        distinct = [o for o in entries if o is not parsed and not same_work(o, parsed)]
        same_author = [o for o in distinct if o["authors"][0].split(",")[0].strip().lower() == surname]
        others = [o for o in distinct if o not in same_author]
        pool = same_author + rng.sample(others, min(len(others), max(0, decoys - len(same_author))))
        decoy_works = [_work(o, id=f"decoy-{index}") for index, o in enumerate(pool[:decoys])]
        query = {**parsed, "doi": None}  # title-and-author matching: the hard case
        for name, make in variants.items():
            right = make(parsed)
            if right is None:
                continue
            candidates = decoy_works + [right]
            rng.shuffle(candidates)
            resolution = resolve_candidates(candidates, query)
            chosen = resolution.get("selected_candidate_id")
            outcomes[name]["trials"] += 1
            outcomes[name]["right"] += chosen == "right"
            outcomes[name]["wrong"] += bool(chosen and chosen != "right")
            outcomes[name]["undecided"] += chosen is None
        resolution = resolve_candidates(decoy_works, query)
        outcomes["right_record_absent"]["trials"] += 1
        outcomes["right_record_absent"]["wrong"] += bool(resolution.get("selected_candidate_id"))
    summary = {}
    for name, counts in outcomes.items():
        trials = counts["trials"]
        summary[name] = {"trials": trials, "wrong_match_rate": _ratio(counts["wrong"], trials)}
        if name != "right_record_absent":
            summary[name].update({"right_match_rate": _ratio(counts["right"], trials),
                                  "left_for_review": _ratio(counts["undecided"], trials)})
    return {"entries": len(entries), "decoys_per_entry": decoys, "cases": summary}


# ------------------------------------------------------------ self-citation
SAME_PERSON_FORMS = ("{S}, {I}.", "{I}. {S}", "{S}, {G}", "{G} {S}", "{SU}, {I}.", "{S} {I}")
OTHER_PERSON_FORMS = ("{S}, {X}.", "{X}. {S}", "{T}, {I}.")


def self_citation(baseline: dict, manuscript_authors: list[str]) -> dict:
    people = manuscript_people(manuscript_authors, [])
    counts = Counter()
    for person in people:
        given, surname = (person["name"].split(" ", 1) + [""])[:2] if "," not in person["name"] else \
            (person["name"].split(",", 1)[1].strip(), person["name"].split(",", 1)[0].strip())
        if not surname or not given:
            continue
        initial = given[0]
        other_initial = next(letter for letter in "QXZKJ" if letter != initial.upper())
        values = {"S": surname, "I": initial, "G": given, "SU": surname.upper(), "X": other_initial,
                  "T": surname[:-1] + ("a" if surname[-1] != "a" else "e")}
        for form in SAME_PERSON_FORMS:
            reference = {"original_data": {"parsed": {"authors": ["Someone, A.", form.format(**values)]}}}
            matched = classify_reference(people, set(), reference)["is_self_citation"]
            counts["same_trials"] += 1
            counts["same_found"] += matched
        for form in OTHER_PERSON_FORMS:
            reference = {"original_data": {"parsed": {"authors": [form.format(**values)]}}}
            matched = classify_reference(people, set(), reference)["is_self_citation"]
            counts["other_trials"] += 1
            counts["other_flagged"] += matched
    return {"manuscript_authors": len(people),
            "same_person_found": _ratio(counts["same_found"], counts["same_trials"]),
            "same_person_trials": counts["same_trials"],
            "different_person_flagged": _ratio(counts["other_flagged"], counts["other_trials"]),
            "different_person_trials": counts["other_trials"]}


# ------------------------------------------------------------------- runner
def run(source: dict, *, manuscript_authors: Optional[list[str]] = None, seed: int = 7, rounds: int = 5,
        per_round: int = 20, drop_trials: int = 40) -> dict:
    baseline = _anchor(source)
    references = baseline["references_list"]
    report = {
        "benchmark_version": BENCHMARK_VERSION, "seed": seed,
        "document": {"pages": len(source["pages"]), "references": len(references),
                     "citation_occurrences": len(baseline["citations_in_text"]),
                     "bibliography_style": baseline["extraction_metadata"].get("bibliography_style") or "numbered"},
        "metadata_differences": metadata_differences(baseline, seed=seed),
        "identity_resolution": identity_resolution(baseline, seed=seed),
    }
    if report["document"]["bibliography_style"] == "author_year":
        report["citation_year_typos"] = year_typos(source, baseline, per_round=per_round, rounds=rounds, seed=seed)
    report["dropped_citations"] = dropped_citations(source, baseline, trials=drop_trials, seed=seed)
    if manuscript_authors:
        report["self_citation"] = self_citation(baseline, manuscript_authors)
    return report


__all__ = ["BENCHMARK_VERSION", "dropped_citations", "identity_resolution", "metadata_differences", "run",
           "self_citation", "year_typos"]
