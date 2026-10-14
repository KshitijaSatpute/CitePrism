"""Regressions from a German edited volume (update 5.3.1).

Two references printed correct DOIs yet ended as "Source provider unavailable":
the index stored the subtitle separately (so the titles looked different), one
DOI named a chapter of the cited book, and a rate-limited index elsewhere then
turned "no confident match" into "provider unavailable".
"""

import unittest

from src.evidence.consistency import compare
from src.evidence.identity import title_similarity
from src.evidence.providers import ProviderFailure
from src.evidence.resolution import resolve_with_providers
from src.evidence.review import build_review, describe_difference, provider_outage
from src.evidence.scholarly import _with_subtitle


class Stub:
    def __init__(self, results=None, error=None):
        self.results, self.error = results or [], error

    def search(self, parsed):
        if self.error:
            raise ProviderFailure(self.error, "stub")
        return self.results


def record(**fields):
    base = {"_provider": "crossref", "authors": [], "publication_year": 2024}
    base.update(fields)
    return base


AXELSSON = {"title": "Bildung, Praxistransfer und Kooperation. Kompetenzentwicklung für die Hochschullehre "
                     "in Netzwerken", "authors": ["Axelsson, C.", "Blume, D.", "Volk, B."], "year": 2024,
            "doi": "10.14361/9783839471807"}
LUDWIG = {"title": "Organisationales Lernen und Digitalisierung. Lernen in und von Organisationen unter dem "
                   "Einfluss der Digitalisierung", "authors": ["Ludwig, A."], "year": 2024,
          "doi": "10.1007/978-3-658-44153-1_3"}
RATE_LIMITED = Stub(error="rate_limit")


class TitleTests(unittest.TestCase):
    def test_a_dropped_subtitle_is_not_a_different_title(self):
        self.assertGreaterEqual(title_similarity(AXELSSON["title"], "Bildung, Praxistransfer und Kooperation"), .8)
        self.assertLess(title_similarity(AXELSSON["title"], "Organisationales Lernen und Digitalisierung"), .8)

    def test_crossref_subtitles_are_joined(self):
        self.assertEqual(_with_subtitle(["Bildung"], ["Kompetenz"]), "Bildung: Kompetenz")
        self.assertEqual(_with_subtitle(["Bildung: Kompetenz"], ["Kompetenz"]), "Bildung: Kompetenz")
        self.assertIsNone(_with_subtitle([], None))


class ResolutionTests(unittest.TestCase):
    def test_printed_doi_resolves_even_when_another_index_is_rate_limited(self):
        crossref = Stub([record(doi=AXELSSON["doi"], title="Bildung, Praxistransfer und Kooperation",
                                authors=[{"display_name": "Axelsson, Charlotte"}])])
        resolution = resolve_with_providers(AXELSSON, {"crossref": crossref, "semantic_scholar": RATE_LIMITED})
        self.assertEqual((resolution["status"], resolution["method"]), ("resolved", "doi_exact"))

    def test_a_chapter_doi_of_the_cited_book_resolves_and_is_reported(self):
        chapter = record(doi=LUDWIG["doi"], title="Lernen in Organisationen: eine Einordnung",
                         authors=[{"display_name": "Ludwig, Anna"}],
                         primary_location={"source": {"display_name": "Organisationales Lernen und Digitalisierung"}})
        resolution = resolve_with_providers(LUDWIG, {"crossref": Stub([chapter]), "semantic_scholar": RATE_LIMITED})
        self.assertEqual(resolution["status"], "resolved")
        source = {"doi": LUDWIG["doi"], "title": chapter["title"], "year": 2024,
                  "venue": "Organisationales Lernen und Digitalisierung", "authors": chapter["authors"]}
        differences = compare(LUDWIG, source, printed_doi_status="registered")
        self.assertEqual([(d["field"], d["severity"]) for d in differences], [("doi", "minor")])
        self.assertIn("one chapter", differences[0]["detail"])

    def test_a_doi_of_a_different_work_is_a_manuscript_finding_not_an_outage(self):
        other = record(doi=LUDWIG["doi"], title="Quantum dots in photovoltaics",
                       authors=[{"display_name": "Zhang, Wei"}])
        resolution = resolve_with_providers(LUDWIG, {"crossref": Stub([other]), "semantic_scholar": RATE_LIMITED})
        self.assertEqual(resolution["status"], "unresolved")
        self.assertEqual(resolution["printed_doi_record"]["title"], "Quantum dots in photovoltaics")

    def test_an_outage_without_any_doi_match_still_reports_unavailable(self):
        resolution = resolve_with_providers(dict(LUDWIG, doi=None), {"crossref": Stub([]),
                                                                     "semantic_scholar": RATE_LIMITED})
        self.assertEqual(resolution["status"], "provider_unavailable")
        self.assertIn("Semantic Scholar was limiting requests", provider_outage(resolution))


class SemanticScholarTierTests(unittest.TestCase):
    def config(self, key=None):
        from types import SimpleNamespace
        return SimpleNamespace(RESOLUTION_PROVIDERS=("openalex", "crossref", "semantic_scholar"),
                               SUPPLEMENTARY_PROVIDERS=(), SEMANTIC_SCHOLAR_API_KEY=key, OFFLINE_MODE=False,
                               CROSSREF_MAILTO=None, SEMANTIC_SCHOLAR_BASE_URL="https://example.invalid",
                               CROSSREF_BASE_URL="https://example.invalid")

    def test_keyless_semantic_scholar_is_asked_only_for_unresolved_references(self):
        from src.evidence.scholarly import build_clients, build_supplementary_clients
        keyless = self.config()
        self.assertEqual(sorted(build_clients(keyless, None)), ["crossref"])
        self.assertEqual(sorted(build_supplementary_clients(keyless, None)), ["semantic_scholar"])
        keyed = self.config("k")
        self.assertEqual(sorted(build_clients(keyed, None)), ["crossref", "semantic_scholar"])
        self.assertEqual(build_supplementary_clients(keyed, None), {})


class WordingTests(unittest.TestCase):
    def test_missing_authors_are_named_and_repository_venues_ignored(self):
        printed = {"title": "Cultivating communities of practice", "year": 2002, "venue": "Harvard Business School",
                   "authors": ["Wenger, E.", "McDermott, R.", "Snyder, W. M."]}
        source = {"title": "Cultivating communities of practice", "year": 2002,
                  "venue": "CERN Document Server (European Organization for Nuclear Research)",
                  "authors": [{"display_name": "Etienne Wenger"}, {"display_name": "William M. Snyder"}]}
        differences = compare(printed, source)
        self.assertEqual([d["field"] for d in differences], ["authors"])
        # McDermott is a real co-author: the record lists 2 of the 3 printed authors and both are
        # printed, so the record is short — a note, not a difference in the entry.
        self.assertEqual(differences[0]["severity"], "info")
        self.assertIn("lists 2 of the 3 printed authors; it leaves out McDermott, R", differences[0]["detail"])
        source["authors"].append({"display_name": "Jean Lave"})
        differences = compare(printed, source)
        self.assertEqual(describe_difference(differences[0]),
                         "Authors: printed but not on the source record: McDermott, R. "
                         "(the database may list fewer authors)")
        self.assertNotIn("None", describe_difference(differences[0]))

    def test_doi_conflict_reaches_the_reviewer_as_a_manuscript_finding(self):
        from src.evidence import SCHEMA_VERSION
        ref = {"reference_uid": "u1", "ref_id": "Ludwig 2024",
               "original_data": {"ref_id": "Ludwig 2024", "extraction_status": "located", "parsed": LUDWIG},
               "resolution": {"status": "unresolved", "printed_doi_record": {"title": "Quantum dots", "year": 2019},
                              "provider_errors": [{"provider": "semantic_scholar", "error_code": "rate_limit"}]},
               "external_metadata": {}, "citation_count": 1, "evidence_status": "not_assessed",
               "claim_assessment": "not_assessed", "claim_assessments": []}
        review = build_review({"schema_version": SCHEMA_VERSION, "scored_references": [ref],
                               "citations_in_text": []})
        finding = next(f for f in review["findings"] if f["category"] == "printed_doi_conflict")
        self.assertFalse(finding["system_issue"])
        self.assertIn("Quantum dots", finding["explanation"])


if __name__ == "__main__":
    unittest.main()
