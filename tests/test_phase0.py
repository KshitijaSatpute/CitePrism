from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from database_manager import DatabaseManager
from config.settings import Config
from pipeline_orchestrator import PipelineOrchestrator, merge_stage_result
from src.extractors.gemini_extractor import GoogleInterface, extract_doi_from_text
from src.utils.reference_matching import select_best_candidate
from src.features.reference_risk_summary import ReferenceRiskSummarizer
from src.risk_detection.risk_detector import run_risk_detection
from src.utils.safe_io import atomic_copy_file, atomic_write_json, sha256_file, validate_pdf
from src.utils.scoring import is_clean, is_flagged, numeric_score
from src.scorers.relevance_scorer import canonical_ref_id


class FakeDatabase:
    def __init__(self):
        self.by_hash = {}

    def register_pdf(self, path, original_filename=None):
        digest = sha256_file(path)
        if digest in self.by_hash:
            return self.by_hash[digest], False
        document_id = len(self.by_hash) + 1
        self.by_hash[digest] = document_id
        return document_id, True


class PhaseZeroTests(unittest.TestCase):
    def test_stage_results_accumulate(self):
        result = {
            "success": True,
            "stages_completed": ["registration"],
            "stages_skipped": [],
            "errors": [],
            "file_paths": {"pdf": "paper.pdf"},
            "timings": {},
        }
        merge_stage_result(
            result,
            {
                "success": True,
                "stages_completed": ["parsing"],
                "file_paths": {"parsed": "paper.json"},
            },
        )
        self.assertEqual(result["stages_completed"], ["registration", "parsing"])
        self.assertEqual(
            result["file_paths"], {"pdf": "paper.pdf", "parsed": "paper.json"}
        )

    def test_configuration_validation_is_explicit(self):
        original_provider = Config.LLM_PROVIDER
        original_key = Config.GOOGLE_API_KEY
        try:
            Config.LLM_PROVIDER = "google"
            Config.GOOGLE_API_KEY = None
            with self.assertRaisesRegex(ValueError, "GOOGLE_API_KEY"):
                Config.validate()
        finally:
            Config.LLM_PROVIDER = original_provider
            Config.GOOGLE_API_KEY = original_key

    def test_content_addressing_prevents_same_name_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw"
            config = SimpleNamespace(
                RAW_PDFS_DIR=raw,
                PARSED_DIR=root / "parsed",
                ENRICHED_DIR=root / "enriched",
                SCORED_DIR=root / "scored",
                MAX_PDF_BYTES=1024,
            )
            orchestrator = PipelineOrchestrator(FakeDatabase(), config)
            first_dir, second_dir, third_dir = root / "one", root / "two", root / "three"
            first_dir.mkdir()
            second_dir.mkdir()
            third_dir.mkdir()
            first = first_dir / "paper.pdf"
            second = second_dir / "paper.pdf"
            duplicate = third_dir / "renamed.pdf"
            first.write_bytes(b"%PDF-1.4\nfirst\n%%EOF\n")
            second.write_bytes(b"%PDF-1.4\nsecond\n%%EOF\n")
            duplicate.write_bytes(first.read_bytes())

            first_result = orchestrator._stage_register_pdf(first)
            second_result = orchestrator._stage_register_pdf(second)
            duplicate_result = orchestrator._stage_register_pdf(duplicate)

            self.assertNotEqual(
                first_result["file_paths"]["pdf"], second_result["file_paths"]["pdf"]
            )
            self.assertEqual(first.read_bytes(), b"%PDF-1.4\nfirst\n%%EOF\n")
            self.assertEqual(
                first_result["file_paths"]["pdf"],
                duplicate_result["file_paths"]["pdf"],
            )
            self.assertEqual(len(list(raw.glob("*.pdf"))), 2)

    def test_pdf_validation_rejects_spoofed_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fake.pdf"
            path.write_bytes(b"not a pdf")
            with self.assertRaisesRegex(ValueError, "signature"):
                validate_pdf(path, max_bytes=1024)

    def test_atomic_json_replaces_complete_document(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.json"
            atomic_write_json(path, {"version": 1})
            atomic_write_json(path, {"version": 2, "complete": True})
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"version": 2, "complete": True},
            )

    def test_atomic_file_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            destination = root / "stored" / "paper.pdf"
            source.write_bytes(b"%PDF-1.4\ncomplete\n%%EOF\n")
            atomic_copy_file(source, destination)
            self.assertEqual(destination.read_bytes(), source.read_bytes())

    def test_risk_summary_reads_real_flags_and_empty_is_unknown(self):
        summarizer = ReferenceRiskSummarizer()
        empty = summarizer.summarize([], current_year=2026)
        self.assertEqual(empty["risk_level"], "Unknown")
        self.assertIsNone(empty["risk_score"])

        summary = summarizer.summarize(
            [
                {
                    "RS_final": 75,
                    "label": "relevant",
                    "quality_flags": ["METADATA MISMATCH"],
                    "original_data": {"parsed": {"title": "Example"}},
                }
            ],
            current_year=2026,
        )
        self.assertEqual(summary["counts"]["metadata_mismatches"], 1)

        incomplete = summarizer.summarize(
            [{"RS_final": None, "original_data": {"parsed": {}}}],
            current_year=2026,
        )
        self.assertEqual(incomplete["risk_level"], "Incomplete")
        self.assertIsNone(incomplete["risk_score"])

    def test_risk_detection_preserves_existing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scored.json"
            atomic_write_json(
                path,
                {
                    "manuscript_metadata": {"authors": ["Ada Lovelace"]},
                    "scored_references": [
                        {
                            "quality_flags": ["score_discrepancy"],
                            "self_citation": {"confidence": 0.7},
                            "original_data": {
                                "parsed": {"authors": ["A. Lovelace"], "doi": None}
                            },
                        }
                    ],
                },
            )
            self.assertTrue(run_risk_detection(path))
            ref = json.loads(path.read_text(encoding="utf-8"))["scored_references"][0]
            self.assertIn("score_discrepancy", ref["quality_flags"])
            self.assertIn("MISSING DOI", ref["quality_flags"])
            self.assertEqual(ref["self_citation"]["confidence"], 0.7)

    def test_long_paragraph_respects_chunk_limit(self):
        interface = object.__new__(GoogleInterface)
        chunks = interface._chunk_text("x" * 25_000, 10_000)
        self.assertEqual([len(chunk) for chunk in chunks], [10_000, 10_000, 5_000])

    def test_reference_doi_is_not_assigned_to_manuscript(self):
        text = "A manuscript without a DOI.\n\nReferences\nSmith. 10.1234/reference-only"
        self.assertIsNone(extract_doi_from_text(text))

    def test_citation_regex_rejects_yearly_observations(self):
        interface = object.__new__(GoogleInterface)
        text = (
            "Results (Group A, 2020 observations) were stable. "
            "Table 2 reports 2021 values. Prior work (Smith, 2020) agrees."
        )
        markers = [item["marker"] for item in interface._extract_citations_regex(text)]
        self.assertEqual(markers, ["(Smith, 2020)"])

    def test_missing_score_is_neither_flagged_nor_clean(self):
        reference = {"RS_final": None}
        self.assertIsNone(numeric_score(reference))
        self.assertFalse(is_flagged(reference, 40))
        self.assertFalse(is_clean(reference, 40))

    def test_delete_removes_only_managed_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data" / "raw_pdfs"
            data.mkdir(parents=True)
            pdf = data / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\nmanaged\n%%EOF\n")
            manager = DatabaseManager(root / "database" / "citeprism.db")
            document_id, _ = manager.register_pdf(pdf)

            outside = root / "must-not-delete.txt"
            outside.write_text("preserve", encoding="utf-8")
            with manager._get_connection() as connection:
                connection.execute(
                    "UPDATE documents SET parsed_path = ? WHERE id = ?",
                    (str(outside), document_id),
                )
                connection.commit()

            self.assertTrue(manager.delete_document(document_id))
            self.assertFalse(pdf.exists())
            self.assertTrue(outside.exists())
            outside.unlink()

    def test_existing_database_migrates_original_filename_column(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "database" / "citeprism.db"
            db_path.parent.mkdir(parents=True)
            connection = sqlite3.connect(db_path)
            connection.execute(
                "CREATE TABLE documents ("
                "id INTEGER PRIMARY KEY, pdf_filename TEXT NOT NULL UNIQUE, "
                "pdf_hash TEXT NOT NULL UNIQUE, pdf_path TEXT NOT NULL)"
            )
            connection.commit()
            connection.close()

            manager = DatabaseManager(db_path)
            with manager._get_connection() as migrated:
                columns = {
                    row[1] for row in migrated.execute("PRAGMA table_info(documents)")
                }
            self.assertIn("original_filename", columns)

    def test_metadata_resolution_ranks_candidates_and_abstains(self):
        parsed = {"title": "A Trustworthy Citation System", "year": 2025}
        candidates = [
            {"title": "Unrelated Biology", "publication_year": 2025},
            {"title": "A Trustworthy Citation System", "publication_year": 2025},
        ]
        selected = select_best_candidate(candidates, parsed, 0.7)
        self.assertEqual(selected["title"], "A Trustworthy Citation System")
        self.assertEqual(selected["_match_method"], "title_year_ranked")
        self.assertIsNone(
            select_best_candidate(candidates[:1], parsed, 0.7)
        )

    def test_reference_id_normalization_is_superficial_only(self):
        self.assertEqual(canonical_ref_id(" [ 12 ] "), "12")
        self.assertEqual(canonical_ref_id("Smith-2020"), "smith-2020")


if __name__ == "__main__":
    unittest.main()
