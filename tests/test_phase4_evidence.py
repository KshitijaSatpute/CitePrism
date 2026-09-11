"""OCR detection, full-text scope, multi-provider resolution, insights, evaluation."""

import unittest

from src.evaluation.dataset import DATASET_SCHEMA_VERSION, EvaluationDataset, LabeledItem
from src.evaluation.metrics import (
    agreement,
    binary_report,
    brier_score,
    calibration_bins,
    classification_report,
    expected_calibration_error,
)
from src.evidence.fulltext import FullTextAcquirer, strip_reference_section
from src.evidence.ocr import assess_text_layer, prepare_source
from src.evidence.providers import ProviderFailure
from src.evidence.resolution import merge_key, merge_records, resolve_with_providers
from src.evidence.scholarly import CrossrefClient, SemanticScholarClient, strip_markup
from src.webapi.insights import author_overlap, missing_citations, normalize_name


def page(number, characters):
    return {"page": number, "text": "x" * characters}


class OcrTests(unittest.TestCase):
    def test_text_layer_classification(self):
        self.assertEqual(assess_text_layer({"pages": [page(1, 500), page(2, 400)]}).status, "text")
        # No extractable characters at all is "empty"; a few stray characters
        # is still a scan, so it reports "needs_ocr".
        self.assertEqual(
            assess_text_layer({"pages": [page(1, 0), page(2, 0)]}).status, "empty"
        )
        self.assertEqual(
            assess_text_layer({"pages": [page(1, 0), page(2, 3)]}).status, "needs_ocr"
        )
        self.assertEqual(
            assess_text_layer({"pages": [page(1, 5), page(2, 5), page(3, 900)]}).status,
            "needs_ocr",
        )
        mixed = assess_text_layer({"pages": [page(1, 900), page(2, 0), page(3, 900)]})
        self.assertEqual(mixed.status, "mixed")
        self.assertEqual(mixed.pages_needing_ocr(), [2])

    def test_ocr_disabled_reports_the_condition_rather_than_failing_silently(self):
        class Settings:
            ENABLE_OCR = False
            OCR_ENGINE = "tesseract"
            OCR_MIN_CHARS_PER_PAGE = 120
            OCR_MIN_TEXT_PAGE_RATIO = 0.6

        source = {"pages": [page(1, 0), page(2, 2)]}
        result, status = prepare_source("x.pdf", source, Settings)
        self.assertIs(result, source, "the source must not be modified when OCR is off")
        self.assertEqual(status["text_layer_status"], "needs_ocr")
        self.assertIn("OCR", status["reason"])

    def test_a_text_document_is_left_alone(self):
        class Settings:
            ENABLE_OCR = True
            OCR_ENGINE = "tesseract"
            OCR_MIN_CHARS_PER_PAGE = 120
            OCR_MIN_TEXT_PAGE_RATIO = 0.6

        source = {"pages": [page(1, 900), page(2, 900)]}
        result, status = prepare_source("x.pdf", source, Settings)
        self.assertIs(result, source)
        self.assertFalse(status["ocr_applied"])
        self.assertEqual(status["text_layer_status"], "text")


class FullTextTests(unittest.TestCase):
    def test_a_trailing_reference_list_is_removed(self):
        body = "Body sentence. " * 200 + "\n\nREFERENCES\n\n[1] Someone. A paper. 2020."
        trimmed, changed = strip_reference_section(body)
        self.assertTrue(changed)
        self.assertNotIn("[1] Someone", trimmed)

    def test_an_early_heading_is_not_treated_as_the_boundary(self):
        body = "References\n\n" + "Body sentence. " * 400
        _trimmed, changed = strip_reference_section(body)
        self.assertFalse(changed)

    def test_only_https_open_locations_are_considered(self):
        class Settings:
            ENABLE_FULL_TEXT_ACQUISITION = True
            ENABLE_UNPAYWALL = False
            UNPAYWALL_EMAIL = None
            FULL_TEXT_MAX_BYTES = 1000
            FULL_TEXT_MAX_CHARS = 1000
            RESOLUTION_TIMEOUT_SECONDS = 5

        acquirer = FullTextAcquirer(Settings, recorder=None, session=type("S", (), {"headers": {}})())
        locations = acquirer.candidate_locations(
            {"open_access_pdf_url": "https://arxiv.org/pdf/2403.00001",
             "external_ids": {"ArXiv": "2403.00001"}}, {}
        )
        self.assertTrue(all(item["url"].startswith("https://") for item in locations))
        self.assertEqual(
            acquirer.candidate_locations({"open_access_pdf_url": "http://insecure/x.pdf"}, {}), []
        )

    def test_acquisition_is_off_by_default(self):
        class Settings:
            ENABLE_FULL_TEXT_ACQUISITION = False

        acquirer = FullTextAcquirer(Settings, None, type("S", (), {"headers": {}})())
        self.assertIsNone(acquirer.acquire({}, {}))


class ResolutionTests(unittest.TestCase):
    def crossref(self):
        return CrossrefClient.normalize({
            "DOI": "10.1234/ABC", "title": ["Citation audit at scale"],
            "author": [{"family": "Lovelace", "given": "A"}],
            "issued": {"date-parts": [[2024, 3]]}, "container-title": ["Journal"],
            "abstract": "<jats:p>Short abstract.</jats:p>", "URL": "https://doi.org/10.1234/abc",
        })

    def semantic_scholar(self):
        return SemanticScholarClient.normalize({
            "paperId": "abc123", "externalIds": {"DOI": "10.1234/abc", "ArXiv": "2403.00001"},
            "title": "Citation audit at scale",
            "abstract": "A considerably longer abstract that carries more detail.",
            "year": 2024, "venue": "Journal", "url": "https://s2.org/abc123",
            "authors": [{"name": "Ada Lovelace"}],
            "openAccessPdf": {"url": "https://arxiv.org/pdf/2403.00001"},
        })

    def test_jats_markup_is_stripped_from_crossref_abstracts(self):
        self.assertEqual(strip_markup("<jats:p>Hello <jats:italic>world</jats:italic></jats:p>"),
                         "Hello world")
        self.assertIsNone(strip_markup(None))

    def test_records_sharing_a_doi_merge_into_one_candidate(self):
        self.assertEqual(merge_key(self.crossref()), merge_key(self.semantic_scholar()))
        merged = merge_records({"doi:10.1234/abc": [
            ("crossref", self.crossref()), ("semantic_scholar", self.semantic_scholar())
        ]})
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["_provider_count"], 2)
        # The richer abstract wins; the open-access location is carried over.
        self.assertIn("considerably longer", merged[0]["abstract"])
        self.assertEqual(merged[0]["open_access_pdf_url"], "https://arxiv.org/pdf/2403.00001")

    def test_provider_disagreement_is_preserved_rather_than_voted_away(self):
        left = self.crossref()
        right = dict(self.semantic_scholar())
        right["publication_year"] = 2023
        merged = merge_records({"doi:10.1234/abc": [
            ("crossref", left), ("semantic_scholar", right)
        ]})
        fields = [item["field"] for item in merged[0]["_provider_disagreements"]]
        self.assertIn("publication_year", fields)

    def test_agreement_is_recorded_as_evidence_on_the_candidate(self):
        class Fake:
            def __init__(self, results):
                self.results = results

            def search(self, _parsed):
                return self.results

        openalex = [{
            "id": "https://openalex.org/W1", "doi": "https://doi.org/10.1234/abc",
            "title": "Citation audit at scale", "publication_year": 2024,
            "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
            "primary_location": {"landing_page_url": "https://x",
                                 "source": {"display_name": "Journal"}},
        }]
        resolution = resolve_with_providers(
            {"title": "Citation audit at scale", "authors": ["Lovelace, A"],
             "year": 2024, "doi": "10.1234/abc"},
            {"crossref": Fake([self.crossref()]),
             "semantic_scholar": Fake([self.semantic_scholar()])},
            openalex_candidates=openalex,
        )
        self.assertEqual(resolution["status"], "resolved")
        self.assertEqual(resolution["resolution_strategy"], "multi_provider_v1")
        selected = next(item for item in resolution["candidates"] if item["selected"])
        self.assertEqual(selected["provider_agreement"], 3)
        self.assertIn("not a calibrated confidence", resolution["note"])

    def test_a_provider_outage_is_not_reported_as_no_such_work(self):
        class Broken:
            def search(self, _parsed):
                raise ProviderFailure("service_unavailable", "crossref")

        resolution = resolve_with_providers(
            {"title": "Nothing will match this title"}, {"crossref": Broken()},
            openalex_candidates=[],
        )
        self.assertEqual(resolution["status"], "provider_unavailable")
        self.assertEqual(resolution["provider_errors"][0]["error_code"], "service_unavailable")

    def test_one_broken_provider_does_not_stop_the_others(self):
        class Broken:
            def search(self, _parsed):
                raise ProviderFailure("rate_limit", "crossref")

        class Working:
            def search(self, _parsed):
                return [SemanticScholarClient.normalize({
                    "paperId": "p1", "externalIds": {"DOI": "10.1234/abc"},
                    "title": "Citation audit at scale", "year": 2024,
                    "authors": [{"name": "Ada Lovelace"}],
                })]

        resolution = resolve_with_providers(
            {"title": "Citation audit at scale", "authors": ["Lovelace, A"],
             "doi": "10.1234/abc", "year": 2024},
            {"crossref": Broken(), "semantic_scholar": Working()},
            openalex_candidates=[],
        )
        self.assertEqual(resolution["status"], "resolved")


class InsightTests(unittest.TestCase):
    def artifact(self):
        return {
            "manuscript_metadata": {"title": "T", "authors": ["Ada Lovelace", "Grace Hopper"]},
            "scored_references": [
                {"ref_id": "[1]", "original_data": {"parsed": {
                    "title": "A", "authors": ["Lovelace, A", "Smith, J"]}}},
                {"ref_id": "[2]", "original_data": {"parsed": {
                    "title": "B", "authors": ["Smith, J", "Lee, R"]}}},
                {"ref_id": "[3]", "original_data": {"parsed": {
                    "title": "C", "authors": ["Nobody, X"]}}},
            ],
            "missing_citations": [
                {"title": "A promising lead", "authors": "Q", "year": "2023", "rationale": "r"},
                {"title": "A", "authors": "Z", "year": "2020", "rationale": "r"},
            ],
            "literature_search_metadata": {"provider": "google", "model": "gemini-2.5-flash"},
        }

    def test_surnames_are_normalized_for_overlap(self):
        self.assertEqual(normalize_name("Chaudhari, Keyur J."), "chaudhari")
        self.assertEqual(normalize_name("Ada Lovelace"), "lovelace")
        self.assertEqual(normalize_name(""), "")

    def test_overlap_counts_and_refuses_to_draw_a_conclusion(self):
        result = author_overlap(self.artifact())
        self.assertEqual(result["counts"]["total_references"], 3)
        self.assertEqual(result["counts"]["references_sharing_a_manuscript_author"], 1)
        repeated = {node["label"]: node["reference_count"] for node in result["repeated_authors"]}
        self.assertEqual(repeated.get("Smith, J"), 2)
        self.assertIn("does not measure bias", result["not_a_claim"])

    def test_search_candidates_are_labelled_unverified(self):
        result = missing_citations(self.artifact())
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(result["items"][1]["already_cited"])
        self.assertTrue(all(item["verification_status"].startswith("unverified")
                            for item in result["items"]))
        self.assertIn("may not exist", result["guidance"])

    def test_an_artifact_without_candidates_reports_nothing_generated(self):
        result = missing_citations({"scored_references": []})
        self.assertFalse(result["available"])
        self.assertFalse(result["generated"])


class EvaluationTests(unittest.TestCase):
    def test_classification_report_matches_hand_computed_values(self):
        report = classification_report([
            ("resolved_correct", "resolved_correct"),
            ("resolved_correct", "unresolvable"),
            ("unresolvable", "unresolvable"),
            ("resolved_incorrect", "resolved_correct"),
        ])
        self.assertEqual(report["samples"], 4)
        self.assertEqual(report["accuracy"], 0.5)
        per_label = {item["label"]: item for item in report["per_label"]}
        self.assertEqual(per_label["resolved_correct"]["precision"], 0.5)
        self.assertEqual(per_label["unresolvable"]["recall"], 1.0)

    def test_binary_triage_report(self):
        report = binary_report([(True, True), (True, False), (False, False), (False, True)])
        self.assertEqual(report["precision"], 0.5)
        self.assertEqual(report["recall"], 0.5)

    def test_a_perfectly_calibrated_signal_scores_near_zero(self):
        pairs = [(1.0, True)] * 10 + [(0.0, False)] * 10
        self.assertEqual(brier_score(pairs), 0.0)
        self.assertEqual(expected_calibration_error(pairs, bins=10), 0.0)

    def test_an_overconfident_signal_is_penalized(self):
        pairs = [(0.95, False)] * 8 + [(0.95, True)] * 2
        self.assertGreater(expected_calibration_error(pairs, bins=10), 0.5)
        self.assertGreater(brier_score(pairs), 0.5)

    def test_reliability_bins_cover_the_unit_interval(self):
        table = calibration_bins([(0.1, False), (0.9, True)], bins=5)
        self.assertEqual(len(table), 5)
        self.assertEqual(sum(row["count"] for row in table), 2)

    def test_annotator_agreement(self):
        result = agreement([("a", "a"), ("a", "b"), ("b", "b"), ("b", "b")])
        self.assertEqual(result["observed_agreement"], 0.75)
        self.assertLessEqual(result["kappa"], 1.0)

    def test_dataset_validation_rejects_bad_labels_and_digests(self):
        dataset = EvaluationDataset(
            name="t",
            items=[
                LabeledItem("i1", "a" * 64, "[1]", identity_label="resolved_correct"),
                LabeledItem("i2", "short", "[2]", identity_label="not_a_label"),
                LabeledItem("i3", "a" * 64, "[3]"),
            ],
            schema_version=DATASET_SCHEMA_VERSION,
        )
        problems = dataset.validate()
        self.assertTrue(any("SHA-256" in item for item in problems))
        self.assertTrue(any("not a permitted label" in item for item in problems))
        self.assertTrue(any("at least one label" in item for item in problems))

    def test_a_valid_dataset_summarizes_its_annotators(self):
        dataset = EvaluationDataset(
            name="gold",
            items=[
                LabeledItem("i1", "a" * 64, "[1]", identity_label="resolved_correct",
                            annotator="one"),
                LabeledItem("i2", "a" * 64, "[2]", support_label="supports", annotator="two"),
            ],
        )
        self.assertEqual(dataset.validate(), [])
        self.assertEqual(dataset.summary()["annotators"], ["one", "two"])


if __name__ == "__main__":
    unittest.main()
