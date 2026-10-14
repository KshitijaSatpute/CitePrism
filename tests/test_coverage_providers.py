"""Broader metadata coverage: dblp, Europe PMC and DataCite as supplementary indexes.

Responses follow each API's published JSON format; no network is used.
"""

import unittest
from types import SimpleNamespace

from src.evidence.providers import CallRecorder, ProviderFailure
from src.evidence.resolution import resolve_with_providers
from src.evidence.scholarly import (
    DataCiteClient,
    DblpClient,
    EuropePmcClient,
    build_supplementary_clients,
)

DBLP_RESPONSE = {"result": {"hits": {"@total": "1", "hit": [{"info": {
    "authors": {"author": [{"@pid": "34/7659", "text": "Kaiming He 0001"}, {"@pid": "s/JianSun", "text": "Jian Sun 0001"}]},
    "title": "Deep Residual Learning for Image Recognition.", "venue": "CVPR", "year": "2016",
    "type": "Conference and Workshop Papers", "doi": "10.1109/CVPR.2016.90",
    "ee": "https://doi.org/10.1109/CVPR.2016.90", "url": "https://dblp.org/rec/conf/cvpr/HeZRS16"}}]}}}

EUROPE_PMC_RESPONSE = {"resultList": {"result": [{
    "id": "31234567", "source": "MED", "pmid": "31234567", "pmcid": "PMC6543210",
    "doi": "10.1038/s41586-019-1234-5", "title": "Single-cell atlas of the developing heart.",
    "authorString": "Smith J, Doe A.", "pubYear": "2019",
    "authorList": {"author": [
        {"fullName": "Smith J", "firstName": "Jane", "lastName": "Smith",
         "authorId": {"type": "ORCID", "value": "0000-0002-1825-0097"}},
        {"fullName": "Doe A", "firstName": "Alex", "lastName": "Doe"}]},
    "journalInfo": {"journal": {"title": "Nature"}},
    "abstractText": "<h4>Background</h4>We profiled 100,000 cells across heart development.",
    "pubTypeList": {"pubType": ["research-article", "Journal Article"]}}]}}

DATACITE_RECORD = {"data": {"id": "10.5281/zenodo.1234567", "type": "dois", "attributes": {
    "doi": "10.5281/zenodo.1234567", "titles": [{"title": "Small-object benchmark dataset"}],
    "creators": [{"name": "Meier, Anna", "givenName": "Anna", "familyName": "Meier",
                  "nameIdentifiers": [{"nameIdentifier": "https://orcid.org/0000-0002-1825-0097",
                                       "nameIdentifierScheme": "ORCID"}]}],
    "publisher": {"name": "Zenodo"}, "publicationYear": 2022,
    "descriptions": [{"description": "Images and annotations.", "descriptionType": "Abstract"}],
    "types": {"resourceTypeGeneral": "Dataset"}, "url": "https://zenodo.org/record/1234567"}}}


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            error = RuntimeError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def json(self):
        return self._body


class FakeSession:
    """Routes by URL substring; records every request."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        for fragment, (status, body) in self.routes.items():
            if fragment in url:
                return FakeResponse(status, body)
        return FakeResponse(404, {})


CONFIG = SimpleNamespace(RESOLUTION_CANDIDATES_PER_PROVIDER=5, RESOLUTION_TIMEOUT_SECONDS=5,
                         SUPPLEMENTARY_PROVIDERS=("dblp", "europe_pmc", "datacite"), OFFLINE_MODE=False)


def recorder():
    return CallRecorder(None, max_attempts=1, retry_delay=0)


class NormalizationTests(unittest.TestCase):
    def test_dblp_strips_disambiguation_numbers_and_trailing_period(self):
        work = DblpClient.normalize(DBLP_RESPONSE["result"]["hits"]["hit"][0]["info"])
        self.assertEqual(work["title"], "Deep Residual Learning for Image Recognition")
        self.assertEqual([a["display_name"] for a in work["authors"]], ["Kaiming He", "Jian Sun"])
        self.assertEqual(work["doi"], "10.1109/cvpr.2016.90")
        self.assertEqual(work["publication_year"], 2016)
        self.assertEqual(work["primary_location"]["source"]["display_name"], "CVPR")

    def test_dblp_handles_a_single_author_object(self):
        work = DblpClient.normalize({"title": "Solo.", "authors": {"author": {"text": "Ada Lovelace"}}, "year": "1843"})
        self.assertEqual(work["authors"], [{"display_name": "Ada Lovelace"}])

    def test_europe_pmc_keeps_abstract_orcid_and_identifiers(self):
        work = EuropePmcClient.normalize(EUROPE_PMC_RESPONSE["resultList"]["result"][0])
        self.assertEqual(work["title"], "Single-cell atlas of the developing heart")
        self.assertEqual(work["authors"][0], {"display_name": "Smith, Jane", "orcid": "0000-0002-1825-0097"})
        self.assertNotIn("<h4>", work["abstract"])
        self.assertIn("100,000 cells", work["abstract"])
        self.assertEqual(work["external_ids"], {"PubMed": "31234567", "PubMedCentral": "PMC6543210"})
        self.assertEqual(work["primary_location"]["source"]["display_name"], "Nature")

    def test_europe_pmc_reports_retracted_publications(self):
        record = dict(EUROPE_PMC_RESPONSE["resultList"]["result"][0],
                      pubTypeList={"pubType": ["Retracted Publication"]})
        self.assertTrue(EuropePmcClient.normalize(record)["is_retracted"])

    def test_datacite_reads_creators_orcid_publisher_and_type(self):
        work = DataCiteClient.normalize(DATACITE_RECORD["data"])
        self.assertEqual(work["doi"], "10.5281/zenodo.1234567")
        self.assertEqual(work["authors"][0]["orcid"], "https://orcid.org/0000-0002-1825-0097")
        self.assertEqual(work["primary_location"]["source"]["display_name"], "Zenodo")
        self.assertEqual(work["resource_type"], "Dataset")
        self.assertEqual(work["abstract"], "Images and annotations.")


class ClientRequestTests(unittest.TestCase):
    def test_dblp_queries_the_publication_search(self):
        session = FakeSession({"dblp.org/search/publ/api": (200, DBLP_RESPONSE)})
        results = DblpClient(CONFIG, recorder(), session).search({"title": "Deep residual learning"})
        self.assertEqual(len(results), 1)
        self.assertEqual(session.calls[0][1]["format"], "json")

    def test_europe_pmc_searches_by_doi_then_falls_back_to_title(self):
        session = FakeSession({"europepmc": (200, {"resultList": {"result": []}})})
        EuropePmcClient(CONFIG, recorder(), session).search({"doi": "10.1/x", "title": "A heart atlas"})
        queries = [params["query"] for _, params in session.calls]
        self.assertEqual(queries, ['DOI:"10.1/x"', 'TITLE:"A heart atlas"'])

    def test_datacite_404_on_a_doi_is_not_an_outage(self):
        session = FakeSession({"/dois/": (404, {}), "api.datacite.org/dois": (200, {"data": []})})
        self.assertEqual(DataCiteClient(CONFIG, recorder(), session).search({"doi": "10.1/none"}), [])

    def test_supplementary_clients_are_disabled_offline(self):
        offline = SimpleNamespace(**{**vars(CONFIG), "OFFLINE_MODE": True})
        self.assertEqual(build_supplementary_clients(offline, recorder(), FakeSession({})), {})
        self.assertEqual(set(build_supplementary_clients(CONFIG, recorder(), FakeSession({}))),
                         {"dblp", "europe_pmc", "datacite"})


class StubClient:
    def __init__(self, results=None, error=None):
        self.results, self.error, self.calls = results or [], error, 0

    def search(self, parsed):
        self.calls += 1
        if self.error:
            raise ProviderFailure(self.error, "stub")
        return self.results


PARSED = {"title": "Deep Residual Learning for Image Recognition", "authors": ["He, K.", "Sun, J."], "year": "2016"}
RESNET = DblpClient.normalize(DBLP_RESPONSE["result"]["hits"]["hit"][0]["info"])


class SupplementaryTierTests(unittest.TestCase):
    def test_supplementary_indexes_are_skipped_when_primary_resolves(self):
        dblp = StubClient([RESNET])
        resolution = resolve_with_providers(PARSED, {"crossref": StubClient([dict(RESNET, _provider="crossref")])},
                                            supplementary={"dblp": dblp})
        self.assertEqual(resolution["status"], "resolved")
        self.assertEqual(dblp.calls, 0)
        self.assertEqual(resolution["supplementary_providers_queried"], [])

    def test_supplementary_indexes_resolve_what_primary_cannot(self):
        dblp = StubClient([RESNET])
        resolution = resolve_with_providers(PARSED, {"crossref": StubClient([])}, supplementary={"dblp": dblp})
        self.assertEqual(dblp.calls, 1)
        self.assertEqual(resolution["supplementary_providers_queried"], ["dblp"])
        self.assertEqual(resolution["status"], "resolved")
        self.assertIn("dblp", resolution["provider_candidate_counts"])

    def test_a_correct_arxiv_doi_is_no_longer_reported_as_unregistered(self):
        # Regression: the arXiv DOI conflicts with the journal record's DOI, so the
        # primary indexes leave the reference unresolved and call the DOI
        # unregistered — a false "this reference may not exist" signal.
        preprint_doi = "10.48550/arxiv.1512.03385"
        primary = {"crossref": StubClient([dict(RESNET, _provider="crossref")])}
        before = resolve_with_providers(dict(PARSED, doi=preprint_doi), primary)
        # Absence from the indexes is "not indexed", never "unregistered" (only doi.org can say that).
        self.assertEqual((before["status"], before["printed_doi_status"]), ("unresolved", "not_indexed"))
        datacite = StubClient([{"doi": preprint_doi, "title": PARSED["title"], "_provider": "datacite"}])
        after = resolve_with_providers(dict(PARSED, doi=preprint_doi), primary, supplementary={"datacite": datacite})
        self.assertEqual((after["status"], after["method"], after["printed_doi_status"]),
                         ("resolved", "doi_exact", "registered"))

    def test_datacite_confirms_a_printed_doi_when_the_title_match_has_no_doi(self):
        preprint_doi = "10.5281/zenodo.1234567"
        no_doi = dict(RESNET, doi=None, _provider="crossref")
        datacite = StubClient([{"doi": preprint_doi, "title": "Some dataset"}])
        resolution = resolve_with_providers(dict(PARSED, doi=preprint_doi), {"crossref": StubClient([no_doi])},
                                            supplementary={"datacite": datacite})
        self.assertEqual(resolution["status"], "resolved")
        self.assertEqual(resolution["printed_doi_status"], "registered")
        self.assertEqual(resolution["printed_doi_registry"], "datacite")

    def test_an_unreachable_datacite_makes_the_doi_status_unknown_not_unregistered(self):
        resolution = resolve_with_providers(dict(PARSED, doi="10.48550/arxiv.0000.00000"),
                                            {"crossref": StubClient([dict(RESNET, _provider="crossref")])},
                                            supplementary={"datacite": StubClient(error="service_unavailable")})
        self.assertEqual(resolution["printed_doi_status"], "unknown")


if __name__ == "__main__":
    unittest.main()


class CalibrationTests(unittest.TestCase):
    """Thresholds are tuned on some manuscripts and graded on others."""

    @staticmethod
    def groups(count, seed=7):
        import random
        rng = random.Random(seed)
        result = []
        for _ in range(count):
            group = [(rng.uniform(0, 35), True) for _ in range(3)]       # irrelevant: low scores
            group += [(rng.uniform(45, 100), False) for _ in range(7)]   # relevant: high scores
            group.append((rng.uniform(30, 50), rng.random() < .5))       # ambiguous middle
            result.append(group)
        return result

    def test_cross_validation_reports_held_out_performance(self):
        from src.evaluation.metrics import cross_validated_threshold
        result = cross_validated_threshold(self.groups(12), folds=4)
        self.assertEqual(result["folds"], 4)
        self.assertEqual(result["held_out"]["samples"], 12 * 11)
        self.assertGreater(result["held_out"]["f1"], 0.8)
        self.assertLessEqual(result["held_out"]["f1"], result["in_sample_f1"] + 1e-9)
        self.assertTrue(30 <= result["recommended_threshold"] <= 50)

    def test_calibration_refuses_small_datasets(self):
        import importlib.util, pathlib
        spec = importlib.util.spec_from_file_location(
            "calibrate", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "calibrate_thresholds.py")
        calibrate = importlib.util.module_from_spec(spec); spec.loader.exec_module(calibrate)
        from src.evaluation.metrics import cross_validated_threshold
        small = {"positives": 6, "auroc": 0.9, "cross_validated": cross_validated_threshold(self.groups(2))}
        self.assertFalse(calibrate.recommend(small)["ok"])
        groups = self.groups(12)
        large = {"positives": sum(label for group in groups for _, label in group), "auroc": 0.95,
                 "cross_validated": cross_validated_threshold(groups)}
        result = calibrate.recommend(large)
        self.assertTrue(result["ok"])
        self.assertTrue(result["env"].startswith("BORDERLINE_THRESHOLD="))


class LoadTestSummaryTests(unittest.TestCase):
    def test_percentiles_errors_and_throughput(self):
        import importlib.util, pathlib
        spec = importlib.util.spec_from_file_location(
            "load_test", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "load_test.py")
        load_test = importlib.util.module_from_spec(spec); spec.loader.exec_module(load_test)
        samples = {"review": [(i / 100, True) for i in range(1, 101)] + [(5.0, False)]}
        summary = load_test.summarize(samples, seconds=10)
        self.assertEqual(summary["requests"], 101)
        self.assertEqual(summary["endpoints"]["review"]["errors"], 1)
        self.assertEqual(summary["endpoints"]["review"]["p95_ms"], 950.0)
        self.assertAlmostEqual(summary["error_rate"], round(1 / 101, 4))
        self.assertEqual(summary["throughput_rps"], 10.1)
        self.assertIsNone(load_test.percentile([], .5))
