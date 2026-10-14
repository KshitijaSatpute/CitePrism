"""Workspace fixtures shared by tests that do and do not need the HTTP stack."""

from src.evidence import SCHEMA_VERSION

TENANT = "00000000-0000-4000-8000-000000000001"
OTHER_TENANT = "00000000-0000-4000-8000-000000000002"


def evidence_artifact():
    """An artifact with one system issue, so repairs have something to plan."""
    return {
        "schema_version": SCHEMA_VERSION,
        "manuscript_metadata": {"title": "API contract paper", "authors": ["Ada Lovelace"]},
        "citations_in_text": [],
        "scored_references": [
            {
                "reference_uid": "api-reference",
                "ref_id": "[1]",
                "original_data": {
                    "raw_text": "[1] A source",
                    "parsed": {"title": "A source", "authors": ["Lovelace, A"]},
                    "extraction_status": "located",
                    "extraction_issues": [],
                    "source_locations": [{"page": 2}],
                },
                "resolution": {"status": "resolved", "metadata_differences": []},
                "external_metadata": {"doi": "10.1/example"},
                "citation_count": 1,
                "evidence_status": "assessment_unavailable",
                "claim_assessment": "not_assessed",
                "claim_assessments": [
                    {"task_id": "t1", "status": "assessment_unavailable", "error_code": "timeout"}
                ],
            }
        ],
        "missing_citations": [
            {"title": "A search lead", "authors": "Q", "year": "2024", "rationale": "related"}
        ],
        "literature_search_metadata": {"provider": "google", "model": "gemini-2.5-flash"},
    }
