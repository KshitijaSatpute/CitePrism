"""
CitePrism Phase 4: Self-Citation & Risk Detection
=================================================
Identifies author overlap (Self-Citations) and aggregates risk flags
(Retractions, Metadata Mismatches, Missing DOIs) without penalizing scores.
"""

import json
import logging
from pathlib import Path
from typing import List, Dict
from src.utils.safe_io import atomic_write_json

logger = logging.getLogger(__name__)

def extract_last_names(author_list: List[str]) -> set:
    """Cleans and extracts last names from a list of author strings."""
    last_names = set()
    for author in author_list:
        clean_author = author.replace(',', '').replace('.', '').strip()
        parts = clean_author.split()
        if parts:
            last_names.add(parts[-1].lower())
    return last_names

def detect_self_citation(manuscript_authors: List[str], reference_authors: List[str]) -> bool:
    """Checks for intersection between manuscript authors and reference authors."""
    if not manuscript_authors or not reference_authors:
        return False

    ms_last_names = extract_last_names(manuscript_authors)
    ref_last_names = extract_last_names(reference_authors)

    overlap = ms_last_names.intersection(ref_last_names)
    return bool(overlap)

def aggregate_risk_flags(ref_data: Dict) -> List[str]:
    """Gathers all warnings and errors into a standardized list."""
    flags = []

    ext_metadata = ref_data.get("external_metadata", {})
    if ext_metadata and ext_metadata.get("is_retracted") is True:
        flags.append("RETRACTED PAPER")

    consistency = ref_data.get("consistency_status", "")
    if "Mismatch" in consistency:
        flags.append("METADATA MISMATCH")

    parsed_data = ref_data.get("original_data", {}).get("parsed", {})
    if not parsed_data.get("doi"):
        flags.append("MISSING DOI")

    return flags

def run_risk_detection(scored_json_path: Path):
    """
    Dynamically loads the scored data, applies risk logic, and updates the file in place.
    Called by the Pipeline Orchestrator.
    """
    logger.info(f"Applying risk detection to: {scored_json_path}")

    if not scored_json_path.exists():
        logger.error(f"File not found: {scored_json_path}")
        return False

    with open(scored_json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    from src.evidence import SCHEMA_VERSION
    if data.get("schema_version") == SCHEMA_VERSION:
        from src.evidence.review import build_review
        data["review"] = build_review(data)
        atomic_write_json(scored_json_path, data)
        return True

    manuscript_authors = data.get("manuscript_metadata", {}).get("authors", [])
    scored_refs = data.get("scored_references", [])

    for ref in scored_refs:
        ref_authors = ref.get("original_data", {}).get("parsed", {}).get("authors", [])

        # 1. Detect Self-Citation
        is_self_cite = detect_self_citation(manuscript_authors, ref_authors)
        self_citation = dict(ref.get("self_citation") or {})
        self_citation.setdefault("method", "last_name_overlap")
        self_citation["is_self_cite"] = is_self_cite
        ref["self_citation"] = self_citation

        # 2. Aggregate Quality Flags
        existing_flags = list(ref.get("quality_flags") or [])
        risk_flags = aggregate_risk_flags(ref)
        ref["quality_flags"] = list(dict.fromkeys(existing_flags + risk_flags))

    # Save the updated data back to the same file dynamically
    data["scored_references"] = scored_refs
    atomic_write_json(scored_json_path, data)

    logger.info("Risk detection complete and appended to scored JSON.")
    return True
