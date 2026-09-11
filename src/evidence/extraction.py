"""Preserve printed entries and locate citation occurrences in extracted page text."""

import copy
import re
from collections import Counter
from pathlib import Path

from src.evidence import EXTRACTION_VERSION
from src.evidence.text import bibliographic_text, canonical_id, digest_json, digest_text, marker_ids

REFERENCE_HEADER = re.compile(
    r"(?im)^[ \t]*(?:(?:\d+(?:\.\d+)*[.)]?|[IVX]+[.)])\s*)?"
    r"(?:references|bibliography|works\s+cited)[ \t]*\r?$"
)
ENTRY_MARKER = re.compile(r"(?m)^[ \t]*(\[\d+\]|\(\d+\)|\d+[.)])[ \t]+")
NUMERIC_CITATION = r"\[\s*\d+(?:\s*(?:[,;–—-])\s*\d+)*\s*\]"
AUTHOR_CITATION = (
    r"\([A-Z][A-Za-zÀ-ÖØ-öø-ÿ-]+(?:\s+et\s+al\.)?"
    r"(?:\s*(?:,|&|and)\s*[A-Z][A-Za-zÀ-ÖØ-öø-ÿ-]+)*,\s*\d{4}[a-z]?"
    r"(?:,\s*(?:pp?\.)?\s*\d+(?:[-–]\d+)?)?\)"
    r"|\b[A-Z][A-Za-zÀ-ÖØ-öø-ÿ-]+(?:\s+(?:&|and)\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ-]+)?"
    r"(?:\s+et\s+al\.)?\s*\(\d{4}[a-z]?\)"
)
CITATION_PATTERN = re.compile(f"{NUMERIC_CITATION}|{AUTHOR_CITATION}")


def pdf_source(path, method="pypdf"):
    import pypdf

    path = Path(path)
    pages = []
    reader = pypdf.PdfReader(path)
    if reader.is_encrypted:
        raise ValueError("Encrypted PDF: provide an unlocked copy for extraction.")
    for index, page in enumerate(reader.pages, 1):
        if method == "pdfminer":
            from pdfminer.high_level import extract_text
            text = extract_text(str(path), page_numbers=[index - 1])
        else:
            text = page.extract_text() or ""
        pages.append({"page": index, "text": text, "text_sha256": digest_text(text)})
    return {"pdf_sha256": __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
            "method": method, "coordinate_system": "extracted_page_text_characters",
            "pages": pages}


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
    matches = list(REFERENCE_HEADER.finditer(text))
    return matches[-1] if matches else None


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


def anchor_manuscript(parsed, source):
    """Model metadata is an observation; printed slots and positions remain authoritative."""
    result = copy.deepcopy(parsed)
    text, pages = joined_source(source)
    boundary = reference_boundary(text)
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
    slots = list(ENTRY_MARKER.finditer(text, boundary.end())) if boundary else []
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
