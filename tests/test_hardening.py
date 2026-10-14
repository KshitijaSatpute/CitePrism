"""Regressions for the defects found by the external examination of 5.4.

Each test names the finding it guards (H = examiner's high, M = moderate,
A = adversarial tester). A failing test here means a defect has come back.
"""

import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.evidence import SCHEMA_VERSION
from src.evidence import bibliography as bib
from src.evidence import extraction
from src.evidence.analysis import analyze_artifact
from src.evidence.consistency import compare
from src.evidence.extraction import CITATION_PATTERN, anchor_manuscript, numbered_slots, pdf_source, read_pdf_isolated
from src.evidence.identity import resolve_candidates
from src.evidence.relevance import assess_relevance, needs_relevance_review
from src.evidence.resolution import resolve_with_providers
from src.evidence.review import build_review
from src.evidence.self_citation import analyze_self_citations, person_key
from src.evidence.text import digest_text, marker_ids
from src.utils.reference_matching import normalize_doi
from tests.test_coverage_providers import PARSED, RESNET, StubClient
from tests.test_v2_requirements import CONFIG, OTHER_ORCID, VALID_ORCID, grounded, reference

FIXTURES = Path(__file__).parent / "fixtures" / "evidence"
VOLUME = FIXTURES / "edited_volume.pdf"


def timed(function, *args, **kwargs):
    started = time.perf_counter()
    result = function(*args, **kwargs)
    return result, time.perf_counter() - started


def entries(*lines):
    text = "References\n" + "\n".join(lines) + "\n"
    return text, bib.find_sections(text)[0].entries


# ----------------------------------------------------------------- availability
class LinearTimeTests(unittest.TestCase):
    """A1, A2, A3, A8, A18: crafted text must never take more than linear time."""

    def test_citation_scan_does_not_backtrack_on_repeated_connectors(self):
        _, found = entries("Smith, J. (2020). A sufficiently long title about something. Journal, 1(2), 3-4.")
        labels = bib.labels_for(found)
        for body in ("Smith" + " and" * 60 + " x.", "Smith" + "  ,  " * 60 + "x", "Smith" + " von" * 60 + " x"):
            _, seconds = timed(bib.find_citations, body, 0, len(body), found, labels)
            self.assertLess(seconds, 0.5, body[:30])

    def test_numbered_citation_pattern_does_not_backtrack(self):
        body = "(Xa" + "andYa" * 60 + " text"
        _, seconds = timed(lambda: list(CITATION_PATTERN.finditer(body)))
        self.assertLess(seconds, 0.5)

    def test_an_entry_that_never_ends_is_read_in_linear_time(self):
        text = "References\nSmith, J. (2020). Start of an entry that goes on\n" + "and on without ending\n" * 5000
        _, seconds = timed(bib.find_sections, text)
        self.assertLess(seconds, 2.0)

    def test_many_footnotes_on_one_page_stay_linear(self):
        text, found = entries("Smith, J. (2010). A sufficiently long title about something. Journal, 1, 2.")
        page = "Body text.1\n" + "".join(f"{n} Vgl. Smith, 2010.\n" for n in range(1, 2000))
        started = time.perf_counter()
        for match in bib.FOOTNOTE_LINE.finditer(page):
            bib.footnote_anchor(page, match.end())
        self.assertLess(time.perf_counter() - started, 2.0)

    def test_pdf_writer_breaks_a_huge_word_quickly(self):
        from src.workspace.exports import SimplePdf
        pdf = SimplePdf("t")
        _, seconds = timed(pdf.text, "x" * 5000)
        self.assertLess(seconds, 1.0)


class PdfLimitTests(unittest.TestCase):
    """A4: PDFs are read with page, character, time and memory limits."""

    def test_too_many_pages_is_refused_with_a_clear_message(self):
        original = extraction.MAX_PDF_PAGES
        extraction.MAX_PDF_PAGES = 1
        try:
            with self.assertRaises(extraction.PdfLimitError):
                pdf_source(VOLUME)
        finally:
            extraction.MAX_PDF_PAGES = original

    def test_isolated_reading_returns_the_same_pages(self):
        isolated = read_pdf_isolated(VOLUME)
        direct = pdf_source(VOLUME)
        self.assertEqual([page["text_sha256"] for page in isolated["pages"]],
                         [page["text_sha256"] for page in direct["pages"]])

    def test_a_reading_that_takes_too_long_is_stopped(self):
        with self.assertRaises(extraction.PdfLimitError):
            read_pdf_isolated(VOLUME, timeout=0.01)

    def test_invisible_characters_and_surrogates_are_removed(self):
        self.assertEqual(extraction.INVISIBLE.sub("", "Zw\u200bei \u202eetaD"), "Zwei etaD")
        self.assertEqual(extraction.SURROGATES.sub("�", "a\ud800b"), "a�b")

    def test_an_incomplete_file_is_refused_on_upload(self):
        from src.utils.safe_io import validate_pdf
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.pdf"
            path.write_bytes(b"%PDF-1.4\n<html>not a pdf</html>")
            with self.assertRaises(ValueError):
                validate_pdf(path, max_bytes=10_000)
            path.write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n")
            validate_pdf(path, max_bytes=10_000)


# ----------------------------------------------------------------- DOIs
class DoiTests(unittest.TestCase):
    def test_access_notes_are_never_glued_onto_a_doi(self):
        """H2 / A7."""
        for tail in (" [Zugriff: 12.03.2021].", " (accessed 2 May 2021).", " PMID: 31234567.", " [Epub ahead of print].",
                     ". Retrieved from https://x.org", ""):
            raw = f"SMITH, J. (2020). A title. Journal, 1, 2. DOI: 10.1234/abc.567{tail}"
            self.assertEqual(bib.find_doi(raw), "10.1234/abc.567", tail)

    def test_a_doi_broken_across_lines_is_rejoined(self):
        self.assertEqual(bib.find_doi("Wiesbaden: Springer. https://doi.org/10.1007/978-3-658- 44153-1_3."),
                         "10.1007/978-3-658-44153-1_3")

    def test_doi_normalisation(self):
        """H3: prefixes, labels and sentence punctuation do not make a different DOI."""
        for value in ("https://doi.org/10.1109/CVPR.2016.90.", "doi: 10.1109/cvpr.2016.90", " DOI:10.1109/cvpr.2016.90",
                      "https://dx.doi.org/10.1109/cvpr.2016.90"):
            self.assertEqual(normalize_doi(value), "10.1109/cvpr.2016.90")
        self.assertEqual(normalize_doi("10.1002/(SICI)1097-4571"), "10.1002/(sici)1097-4571")

    def test_a_doi_the_entry_does_not_print_is_dropped(self):
        """H1: a model-supplied DOI must be printed in the entry, or it is kept only as an observation."""
        text = ("Introduction. Deep audits matter [1] and so do others [2] [3].\nReferences\n"
                "[1] A. Meier, Deep learning for citation audits. Journal of Tests, 2020.\n"
                "[2] B. Other, Second paper title here. Journal, 2019.\n"
                "[3] C. Third, Third title of paper. Journal, 2018. doi:10.1/printed\n")
        source = {"pdf_sha256": "x", "pages": [{"page": 1, "text": text, "text_sha256": digest_text(text)}]}
        model = {"metadata": {}, "citations_in_text": [], "references_list": [
            {"ref_id": "[1]", "parsed": {"title": "Deep learning for citation audits", "authors": ["A. Meier"],
                                        "year": 2020, "doi": "10.9999/hallucinated"}},
            {"ref_id": "[3]", "parsed": {"title": "Third title of paper", "authors": ["C. Third"], "year": 2018,
                                        "doi": "10.1/printed"}}]}
        result = {item["ref_id"]: item for item in anchor_manuscript(model, source)["references_list"]}
        self.assertIsNone(result["[1]"]["parsed"]["doi"])
        self.assertEqual(result["[1]"]["dropped_model_fields"], ["doi"])
        self.assertEqual(result["[1]"]["extraction_status"], "located")
        self.assertEqual(result["[3]"]["parsed"]["doi"], "10.1/printed")

    def test_absence_from_the_indexes_is_not_unregistered(self):
        """H3: only doi.org can say a DOI does not exist."""
        class Registry:
            def __init__(self, answer):
                self.answer = answer

            def registered(self, doi):
                return self.answer
        primary = {"crossref": StubClient([dict(RESNET, _provider="crossref")])}
        parsed = dict(PARSED, doi="10.3280/xyz.2020.1")
        cases = {None: "not_indexed", True: "registered", False: "unregistered"}
        for answer, status in cases.items():
            resolution = resolve_with_providers(parsed, primary, registry=Registry(answer) if answer is not None else None)
            self.assertEqual(resolution["printed_doi_status"], status, answer)

    def test_not_indexed_is_a_minor_note_not_a_may_not_exist_item(self):
        differences = compare({"doi": "10.3280/x"}, {"doi": "10.1/y"}, printed_doi_status="not_indexed")
        self.assertEqual([(d["field"], d["severity"]) for d in differences], [("doi", "minor")])
        self.assertNotIn("invalid", differences[0]["detail"])


# ----------------------------------------------------------------- names and citations
class NameTests(unittest.TestCase):
    def test_every_script_is_read(self):
        """A6: Š, Ł, Č, Ō, Ş and Cyrillic surnames are entries, not dropped lines."""
        names = ["Adams", "Šmid", "Łukasiewicz", "Čapek", "Ōno", "Şahin", "Иванов", "Zhou"]
        lines = [f"{name}, A. ({2010 + i}). A sufficiently long title number {i} here. Journal, 1, 2."
                 for i, name in enumerate(names)]
        text, found = entries(*lines)
        self.assertEqual([entry.first_surname for entry in found], names)
        body = " ".join(f"{name} ({2010 + i}) showed it." for i, name in enumerate(names))
        cited = bib.find_citations(body, 0, len(body), found, bib.labels_for(found))
        self.assertEqual(len(cited), len(names))

    def test_one_name_parser_handles_every_form(self):
        """H4, H6, M4."""
        cases = {"Smith JA": ("smith", "j"), "He K": ("he", "k"), "Anna Meier1,2": ("meier", "a"),
                 "Dr. Anna Meier": ("meier", "a"), "Anna Meier (corresponding author)": ("meier", "a"),
                 "John Smith Jr.": ("smith", "j"), "James Brown Jr.": ("brown", "j"), "Łukasiewicz, J.": ("łukasiewicz", "j")}
        for name, key in cases.items():
            self.assertEqual(person_key(name), key, name)

    def test_vancouver_names_resolve_an_exact_doi(self):
        """H4: "He K" once made an exact DOI match look like an author conflict."""
        parsed = {"title": "Deep residual learning for image recognition", "authors": ["He K", "Zhang X"],
                  "doi": "10.1109/cvpr.2016.90"}
        candidate = {"id": "W1", "doi": "10.1109/cvpr.2016.90", "title": parsed["title"],
                     "authors": [{"display_name": "Kaiming He"}, {"display_name": "Xiangyu Zhang"}]}
        self.assertEqual(resolve_candidates([candidate], parsed)["status"], "resolved")

    def test_a_discourse_comma_does_not_hide_a_citation(self):
        """M5."""
        _, found = entries("Smith, J. (2010). First sufficiently long title here. Journal, 1, 2.",
                           "Jones, K. (2012). Second sufficiently long title here. Journal, 1, 2.")
        body = "In Germany, Smith (2010) argued it. Later, Jones (2012) disagreed."
        markers = [item["marker"] for item in bib.find_citations(body, 0, len(body), found, bib.labels_for(found))]
        self.assertEqual(markers, ["Smith (2010)", "Jones (2012)"])

    def test_co_authors_are_still_not_citations(self):
        _, found = entries("Axelsson, C., Blume, D. & Volk, B. (2024). A sufficiently long title here. Bielefeld.",
                           "Volk, B. (2024). Another sufficiently long title here. Bielefeld.")
        body = "Wie Axelsson, Blume, Volk, 2024 zeigen."
        markers = [item["marker"] for item in bib.find_citations(body, 0, len(body), found, bib.labels_for(found))]
        self.assertEqual(markers, ["Axelsson, Blume, Volk, 2024"])

    def test_an_attached_superscript_wins_over_a_number_in_the_text(self):
        """M7."""
        page = "Peer feedback supports learning.3 We ran it in 3 schools last year.\n" + "x" * 200 + "\n3 Vgl. Smith, 2010.\n"
        start, end, number = bib.footnote_anchor(page, page.rindex("Vgl."))
        self.assertEqual(page[start - 9:end], "learning.3")

    def test_ieee_ranges_and_phantom_numbers(self):
        """M6, M8."""
        self.assertEqual(marker_ids("[1]–[3]"), ["1", "2", "3"])
        self.assertEqual([m.group() for m in CITATION_PATTERN.finditer("as shown [1]–[3] before")], ["[1]–[3]"])
        text = "References\n[1] A. One, Title, 2016.\n2016. 1, 3\n[2] B. Two, Title, 2017.\n"
        self.assertEqual([m.group(1) for m in numbered_slots(text, 0)], ["[1]", "[2]"])

    def test_capitals_titles_keep_mc_names_and_acronyms(self):
        _, found = entries("WENGER, E., MCDERMOTT, R. & SNYDER, W. M. (2002). Cultivating communities of practice. Boston.",
                           "JOINT RESEARCH CENTRE (JRC) (2017). European framework for the digital competence. Sevilla.")
        self.assertIn("McDermott, R.", found[0].authors)
        self.assertEqual(found[1].organization, "Joint Research Centre (JRC)")


# ----------------------------------------------------------------- outcome semantics
class OutcomeTests(unittest.TestCase):
    def review(self, references, **extra):
        data = {"schema_version": SCHEMA_VERSION, "manuscript_metadata": {"title": "T", "authors": extra.pop("authors", [])},
                "scored_references": references, **extra}
        return build_review(analyze_artifact(data, SimpleNamespace(**vars(CONFIG))))

    def test_topic_similarity_alone_never_reads_as_relevant(self):
        """H5: during a model outage, a similar topic is "topic only", not "relevant"."""
        result = assess_relevance([], {"score": 90.0}, CONFIG)
        self.assertEqual(result["label"], "topic_only")
        from src.assistant import AuditAssistant
        review = self.review([reference(f"r{i}", f"[{i}]", similarity={"score": 90.0}) for i in range(1, 4)])
        self.assertEqual(review["counts"]["relevance_scored"], 0)
        text = AuditAssistant(review).answer("Which references may be irrelevant?")["text"]
        self.assertIn("not judged", text)
        self.assertNotIn("look relevant", text)

    def test_an_on_topic_background_source_is_not_flagged_irrelevant(self):
        """M11."""
        result = assess_relevance([grounded("context_only")], {"score": 10.0}, CONFIG)
        self.assertEqual(result["label"], "borderline")
        self.assertFalse(needs_relevance_review(result))

    def test_irrelevance_counts_come_from_the_findings(self):
        """M1: an unrelated use on an otherwise relevant reference is counted where it is raised."""
        review = self.review([reference("r1", "[1]", assessments=[grounded("supports"), grounded("supports"),
                                                                  grounded("unrelated")], similarity={"score": 90.0})])
        raised = sum(f["category"] == "potentially_irrelevant" for f in review["findings"])
        self.assertEqual((raised, review["counts"]["potentially_irrelevant"]), (1, 1))

    def test_self_citation_without_authors_is_not_assessable(self):
        """H6."""
        result = analyze_self_citations([], [reference("r1", "[1]", authors=["Meier, A."])])
        self.assertEqual(result["status"], "not_assessable")
        from src.assistant import AuditAssistant
        review = self.review([reference("r1", "[1]", authors=["Meier, A."])])
        self.assertIn("could not check self-citation",
                      AuditAssistant(review).answer("Is there too much self-citation?")["text"])

    def test_orcids_are_never_attached_by_position(self):
        """M3: iDs typed in another order must not name the wrong author."""
        refs = [reference(f"r{i}", f"[{i}]", authors=[{"display_name": "Anna Meier", "orcid": VALID_ORCID}])
                for i in range(4)]
        result = analyze_self_citations(["Anna Meier", "Bob Brown"], refs, supplied_orcids=[OTHER_ORCID, VALID_ORCID])
        self.assertEqual(set(result["by_author"]), {"Anna Meier"})

    def test_a_namesake_with_another_orcid_is_not_a_self_citation(self):
        namesake = reference("r1", "[1]", authors=[{"display_name": "Anna Meier", "orcid": OTHER_ORCID}])
        result = analyze_self_citations(["Anna Meier"], [namesake], supplied_orcids=[VALID_ORCID])
        self.assertFalse(result["per_reference"]["r1"]["is_self_citation"])

    def test_missing_literature_count_matches_its_item(self):
        """M2."""
        gaps = {"status": "completed", "counts": {}, "cocitation": {"candidates": [
            {"title": "Strong", "year": 2017, "support_count": 4}, {"title": "Lead", "year": 2018, "support_count": 2}]},
            "suggestions": []}
        review = self.review([reference("r1", "[1]")], literature_gap_analysis=gaps)
        raised = [f for f in review["findings"] if f["category"] == "possibly_missing_literature"]
        self.assertEqual((review["counts"]["possibly_missing_works"], review["counts"]["missing_work_leads"]), (1, 2))
        self.assertEqual(len(raised), 1)

    def test_reference_ages_are_never_negative(self):
        """A13."""
        from src.assistant import AuditAssistant
        review = self.review([reference("r1", "[1]", parsed={"title": "Old", "year": 1990}),
                              reference("r2", "[2]", parsed={"title": "Future", "year": 2099})])
        text = AuditAssistant(review).answer("how old are the references?")["text"]
        self.assertNotIn("-", text.split("Half are")[1].split("years")[0])


class RetryTests(unittest.TestCase):
    """M9: an outage is retried by the next run and never cached."""

    def test_interrupted_stages_are_detected(self):
        from pipeline_orchestrator import interrupted_stage
        with tempfile.TemporaryDirectory() as directory:
            enriched, scored = Path(directory) / "e.json", Path(directory) / "s.json"
            enriched.write_text(json.dumps({"enriched_references": [{"resolution": {"status": "provider_unavailable"}}]}))
            scored.write_text(json.dumps({"scored_references": [{"claim_assessments": [
                {"status": "unavailable", "error_code": "service_unavailable"}]}]}))
            self.assertTrue(interrupted_stage(enriched, "enrichment"))
            self.assertTrue(interrupted_stage(scored, "scoring"))
            enriched.write_text(json.dumps({"enriched_references": [{"resolution": {"status": "resolved"}}]}))
            self.assertFalse(interrupted_stage(enriched, "enrichment"))

    def test_outage_outcomes_are_not_cacheable(self):
        from src.enrichers.openalex_enricher import OpenAlexEnricher
        self.assertFalse(OpenAlexEnricher._cacheable({"resolution": {"status": "provider_unavailable"}}))
        self.assertFalse(OpenAlexEnricher._cacheable({"resolution": {"status": "resolved",
                                                                    "provider_errors": [{"provider": "x"}]}}))
        self.assertTrue(OpenAlexEnricher._cacheable({"resolution": {"status": "resolved"}}))


class LibraryCountTests(unittest.TestCase):
    """A9: superseded decisions and decisions on notes never reduce the open count."""

    def test_open_references_count_current_decisions_only(self):
        from src.workspace.models import AuditOptions
        from src.workspace.service import WorkspaceService
        from src.workspace.store import WorkspaceStore
        from tests.workspace_fixtures import TENANT
        refs = [reference("r1", "[1]", assessments=[grounded("unrelated")], similarity={"score": 10.0}),
                reference("r2", "[2]", status="not_found"),
                reference("r3", "[3]", assessments=[grounded("supports")], similarity={"score": 90.0})]
        artifact = analyze_artifact({"schema_version": SCHEMA_VERSION, "manuscript_metadata": {"title": "T"},
                                     "scored_references": refs}, SimpleNamespace(**vars(CONFIG)))
        projection = build_review(artifact)
        attention = [f for f in projection["findings"] if f.get("reference_id") and f["priority"] != "informational"
                     and not f.get("system_issue")]
        notes = [f for f in projection["findings"] if f["priority"] == "informational"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkspaceStore(root / "w.db")
            service = WorkspaceService(store, data_root=root / "data")
            manuscript, _ = service.register_bytes(TENANT, b"%PDF-1.7\nx\n%%EOF\n", original_filename="x.pdf",
                                                   actor_id="t")
            audit, job = service.create_audit(TENANT, manuscript.id, AuditOptions(), "t")
            store.claim_job(TENANT, job.id, "w")
            path = root / "data" / "scored" / "a.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(artifact))
            summary = dict(projection["counts"], attention_finding_ids=sorted(f["id"] for f in attention))
            store.finish_job(TENANT, job.id, success=True, artifact_path=str(path), summary=summary)
            before = service.list_manuscripts(TENANT)[0].open_findings
            first = store.record_decision(TENANT, audit.id, attention[0]["id"], "dismiss", "fine", "t", "k1")
            for index in range(4):   # the same finding decided again and again
                first = store.record_decision(TENANT, audit.id, attention[0]["id"], "dismiss", "fine", "t",
                                              f"k{index + 2}", first["id"])
            if notes:
                store.record_decision(TENANT, audit.id, notes[0]["id"], "dismiss", "fine", "t", "k9")
            after = service.list_manuscripts(TENANT)[0].open_findings
        open_refs = {f["reference_id"] for f in attention}
        still_open = {f["reference_id"] for f in attention[1:]} | (
            set() if attention[0]["reference_id"] not in {f["reference_id"] for f in attention[1:]} else set())
        self.assertEqual(before, len(open_refs))
        self.assertEqual(after, len(still_open))


if __name__ == "__main__":
    unittest.main()
