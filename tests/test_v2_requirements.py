"""Acceptance tests for the CitePrism V2.0 requirements.

Each test class names the V2.0 statement it verifies, so the traceability matrix
in docs/V2_REQUIREMENTS_TRACEABILITY.md can point at executable evidence.
Every test is offline: provider traffic is simulated and labelled as such.
"""

import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.evidence import SCHEMA_VERSION
from src.evidence.analysis import analyze_artifact
from src.evidence.consistency import assess_consistency, compare, venues_compatible
from src.evidence.literature_gaps import (
    LiteratureGapAnalyzer,
    cocitation_candidates,
    parse_suggestion,
)
from src.evidence.passages import select_passages
from src.evidence.relevance import assess_relevance, needs_relevance_review
from src.evidence.review import build_review
from src.evidence.self_citation import (
    analyze_self_citations,
    normalize_orcid,
    orcids_in_text,
    person_key,
)
from src.evidence.similarity import LexicalBackend, build_similarity

CONFIG = SimpleNamespace(LLM_WEIGHT=0.6, EMBEDDING_WEIGHT=0.4, RELEVANT_THRESHOLD=70,
                         BORDERLINE_THRESHOLD=40)
VALID_ORCID = "0000-0002-1825-0097"
OTHER_ORCID = "0000-0001-5109-3700"


def grounded(decision):
    return {"status": "grounded_proposal", "decision": decision}


def reference(uid, marker, *, authors=(), printed_authors=(), citation_count=1, relevance=None,
              status="resolved", external=None, parsed=None, graph=None, assessments=None,
              similarity=None, snapshot=None):
    external = dict(external or {})
    if authors:
        external["authors"] = list(authors)
    if graph is not None:
        external["graph"] = graph
    return {
        "reference_uid": uid, "ref_id": marker, "citation_count": citation_count,
        "original_data": {"parsed": dict(parsed or {"title": f"Work {uid}", "authors": list(printed_authors)}),
                          "extraction_status": "located", "extraction_issues": []},
        "resolution": {"status": status, "metadata_differences": []},
        "external_metadata": external,
        "relevance": relevance or {},
        "claim_assessments": assessments or [],
        "topic_similarity": similarity,
        "evidence_status": "grounded_proposal" if assessments else "not_assessed",
        "claim_assessment": "not_assessed",
        "source_snapshot": snapshot,
    }


# ---------------------------------------------------------------------------
# Method: "hybrid relevance scoring using LLM judgment and sentence embeddings"
# Objective: "identifies potentially irrelevant references"
# ---------------------------------------------------------------------------
class HybridRelevanceTests(unittest.TestCase):
    def test_final_score_is_the_documented_weighted_combination(self):
        result = assess_relevance([grounded("supports")], {"score": 50.0}, CONFIG)
        self.assertEqual(result["RS_llm"], 100.0)
        self.assertEqual(result["RS_embed"], 50.0)
        self.assertEqual(result["RS_final"], 80.0)  # 0.6 * 100 + 0.4 * 50
        self.assertEqual(result["basis"], "hybrid")
        self.assertEqual(result["label"], "relevant")

    def test_weights_come_from_configuration(self):
        config = SimpleNamespace(**{**vars(CONFIG), "LLM_WEIGHT": 0.5, "EMBEDDING_WEIGHT": 0.5})
        self.assertEqual(assess_relevance([grounded("context_only")], {"score": 30.0}, config)["RS_final"], 40.0)

    def test_ungrounded_and_insufficient_answers_contribute_nothing(self):
        result = assess_relevance(
            [{"status": "grounding_failed", "decision": "supports"}, grounded("insufficient_evidence")],
            None, CONFIG)
        self.assertIsNone(result["RS_llm"])
        self.assertEqual(result["label"], "not_scored")

    def test_unrelated_use_flags_the_reference_even_when_the_mean_is_borderline(self):
        result = assess_relevance([grounded("supports"), grounded("unrelated")], {"score": 40.0}, CONFIG)
        self.assertEqual(result["unrelated_uses"], 1)
        self.assertTrue(needs_relevance_review(result))

    def test_topic_distance_alone_never_labels_a_reference_irrelevant(self):
        result = assess_relevance([], {"score": 5.0}, CONFIG)
        self.assertEqual(result["basis"], "embedding_only")
        self.assertEqual(result["label"], "low_topical_similarity")
        self.assertFalse(needs_relevance_review(result))

    def test_low_grounded_relevance_raises_a_review_item(self):
        data = {"schema_version": SCHEMA_VERSION, "scored_references": [
            reference("r1", "[1]", assessments=[grounded("unrelated")], similarity={"score": 10.0})]}
        review = build_review(analyze_artifact(data, CONFIG))
        categories = [finding["category"] for finding in review["findings"]]
        self.assertIn("potentially_irrelevant", categories)
        self.assertEqual(review["references"][0]["relevance_label"], "potentially_irrelevant")
        self.assertEqual(review["counts"]["potentially_irrelevant"], 1)

    def test_thresholds_apply_without_rescoring(self):
        # RS_llm 33.3 (one supporting use, two unrelated) and topic similarity 80: final 52.
        data = {"schema_version": SCHEMA_VERSION, "scored_references": [
            reference("r1", "[1]", assessments=[grounded("supports"), grounded("unrelated"), grounded("unrelated")],
                      similarity={"score": 80.0})]}
        lenient = analyze_artifact(data, CONFIG)
        strict = analyze_artifact(data, SimpleNamespace(**{**vars(CONFIG), "BORDERLINE_THRESHOLD": 60,
                                                            "RELEVANT_THRESHOLD": 90}))
        self.assertEqual(lenient["scored_references"][0]["relevance_label"], "borderline")
        self.assertEqual(strict["scored_references"][0]["relevance_label"], "potentially_irrelevant")


class SimilarityBackendTests(unittest.TestCase):
    def test_lexical_backend_ranks_related_text_above_unrelated_text(self):
        backend = LexicalBackend()
        related, unrelated = backend.similarities(
            "optical wireless communication for underwater sensor networks",
            ["underwater optical wireless links for sensor nodes and communication",
             "medieval agricultural tax records in northern Europe"])
        self.assertGreater(related.score, unrelated.score)
        self.assertEqual(related.as_dict()["kind"], "lexical_overlap")

    def test_auto_selection_falls_back_to_an_offline_backend(self):
        backend = build_similarity(SimpleNamespace(SIMILARITY_BACKEND="auto", ENABLE_TOPIC_SIMILARITY=True,
                                                   HF_API_TOKEN=None, OFFLINE_MODE=True))
        self.assertIsNotNone(backend)
        self.assertIn(backend.name, {"sentence_transformers", "lexical"})

    def test_similarity_can_be_disabled(self):
        self.assertIsNone(build_similarity(SimpleNamespace(SIMILARITY_BACKEND="off")))


class PassageSelectionTests(unittest.TestCase):
    def test_long_sources_send_the_passage_about_the_claim(self):
        filler = "Introductory remarks about many unrelated matters. " * 400
        target = "The proposed turbo decoder reduced the bit error rate by forty percent in murky water."
        text = filler + target + " " + filler
        selection = select_passages(text, "decoder reduced bit error rate in murky water [7]", budget=6000)
        self.assertIn(target, selection["excerpt"])
        self.assertLessEqual(len(selection["excerpt"]), 6000 + 200)
        for passage in selection["passages"]:
            self.assertIn(text[passage["char_start"]:passage["char_end"]], selection["excerpt"])

    def test_short_sources_are_sent_whole(self):
        self.assertEqual(select_passages("A short abstract.", "anything")["excerpt"], "A short abstract.")


# ---------------------------------------------------------------------------
# Objective: "detects questionable self-citation patterns"
# Extension: "ORCID-based author disambiguation"
# ---------------------------------------------------------------------------
class SelfCitationTests(unittest.TestCase):
    def test_orcid_validation_uses_the_iso_checksum(self):
        self.assertEqual(normalize_orcid(f"https://orcid.org/{VALID_ORCID}"), VALID_ORCID)
        self.assertIsNone(normalize_orcid("0000-0002-1825-0098"))
        self.assertEqual(orcids_in_text(f"ORCID {VALID_ORCID} and 0000-0002-1825-0098"), [VALID_ORCID])

    def test_bibliographic_name_forms_resolve_to_one_key(self):
        keys = {person_key(name) for name in ("Meier, Anna", "Anna Meier", "A. Meier", "Meier A", "Meier AB")}
        self.assertEqual(keys, {("meier", "a")})

    def test_orcid_beats_names_in_both_directions(self):
        same_person = reference("r1", "[1]", authors=[{"display_name": "A. Meier-Schulz", "orcid": VALID_ORCID}])
        namesake = reference("r2", "[2]", authors=[{"display_name": "Anna Meier", "orcid": OTHER_ORCID}])
        result = analyze_self_citations(["Anna Meier"], [same_person, namesake], supplied_orcids=[VALID_ORCID])
        self.assertTrue(result["per_reference"]["r1"]["is_self_citation"])
        self.assertEqual(result["per_reference"]["r1"]["tier"], "orcid")
        self.assertFalse(result["per_reference"]["r2"]["is_self_citation"])

    def test_surname_only_matches_are_shown_but_never_counted(self):
        # Same surname, different initial: a different person, not a match at all.
        result = analyze_self_citations(["Anna Wang"], [reference("r1", "[1]", authors=["Bo Wang"])])
        self.assertEqual(result["per_reference"]["r1"]["tier"], "none")
        no_initial = analyze_self_citations(["Wang"], [reference("r2", "[2]", authors=["Bo Wang"])])
        self.assertEqual(no_initial["per_reference"]["r2"]["tier"], "surname")
        self.assertEqual(no_initial["counts"]["self_citations"], 0)

    def test_patterns_rate_padding_and_concentration(self):
        references = [reference(f"s{i}", f"[{i}]", authors=["Meier, A."], citation_count=0 if i < 2 else 1)
                      for i in range(5)]
        references += [reference(f"o{i}", f"[{i+5}]", authors=["Other, Z."]) for i in range(5)]
        result = analyze_self_citations(["Anna Meier", "Jo Smith"], references)
        codes = {pattern["code"] for pattern in result["patterns"]}
        self.assertEqual(result["counts"]["self_citations"], 5)
        self.assertEqual(result["counts"]["rate"], 0.5)
        self.assertTrue({"high_self_citation_rate", "author_concentration", "uncited_self_citations"} <= codes)
        self.assertEqual(len(result["questionable"]), 2)

    def test_ordinary_self_citation_raises_no_pattern(self):
        references = [reference("s1", "[1]", authors=["Meier, A."])]
        references += [reference(f"o{i}", f"[{i+2}]", authors=["Other, Z."]) for i in range(19)]
        result = analyze_self_citations(["Anna Meier"], references)
        self.assertEqual(result["counts"]["self_citations"], 1)
        self.assertEqual(result["patterns"], [])
        self.assertEqual(result["questionable"], [])

    def test_patterns_and_questionable_items_reach_the_review_queue(self):
        references = [reference(f"s{i}", f"[{i}]", authors=["Meier, A."], citation_count=0) for i in range(4)]
        references += [reference(f"o{i}", f"[{i+4}]", authors=["Other, Z."]) for i in range(4)]
        data = {"schema_version": SCHEMA_VERSION, "manuscript_metadata": {"authors": ["Anna Meier"]},
                "scored_references": references}
        review = build_review(analyze_artifact(data, CONFIG))
        categories = [finding["category"] for finding in review["findings"]]
        self.assertIn("self_citation_pattern", categories)
        self.assertEqual(categories.count("questionable_self_citation"), 4)
        self.assertEqual(review["counts"]["self_citations"], 4)
        self.assertTrue(review["analysis"]["self_citation"]["patterns"])

    def test_front_matter_orcids_are_used_automatically(self):
        data = {"schema_version": SCHEMA_VERSION, "manuscript_metadata": {"authors": ["Anna Meier"]},
                "source_document": {"pages": [{"text": f"Anna Meier, ORCID {VALID_ORCID}"}]},
                "scored_references": [reference("r1", "[1]", authors=[{"display_name": "Meier, A.",
                                                                        "orcid": VALID_ORCID}])]}
        result = analyze_artifact(data, CONFIG)
        self.assertEqual(result["scored_references"][0]["self_citation"]["tier"], "orcid")
        self.assertEqual(result["self_citation_analysis"]["orcids"]["front_matter"], [VALID_ORCID])


# ---------------------------------------------------------------------------
# Objective: "highlights metadata inconsistencies"
# ---------------------------------------------------------------------------
class MetadataConsistencyTests(unittest.TestCase):
    def test_every_bibliographic_field_is_compared(self):
        fields = {difference["field"] for difference in compare(
            {"title": "Something else entirely", "authors": ["Brown, K"], "year": 2015,
             "doi": "10.1/y", "venue": "Nature"},
            {"title": "Deep learning for citation audits", "authors": [{"display_name": "Anna Meier"}],
             "year": 2020, "doi": "10.1/x", "venue": "Science"})}
        self.assertEqual(fields, {"doi", "title", "year", "first_author", "authors", "venue"})

    def test_severity_separates_typos_from_wrong_works(self):
        minor = compare({"title": "Deep learning for citaton audits", "year": 2021},
                        {"title": "Deep learning for citation audits", "year": 2020})
        self.assertEqual({item["severity"] for item in minor}, {"minor"})

    def test_abbreviations_and_acronyms_are_not_inconsistencies(self):
        self.assertTrue(venues_compatible("J. Mach. Learn. Res.", "Journal of Machine Learning Research"))
        self.assertTrue(venues_compatible("Proc. IEEE CVPR", "Proceedings of the IEEE Conference on "
                                          "Computer Vision and Pattern Recognition"))
        self.assertTrue(venues_compatible("NeurIPS", "Advances in Neural Information Processing Systems"))
        self.assertFalse(venues_compatible("Nature", "Science"))

    def test_an_unregistered_doi_on_an_unfound_work_is_a_high_priority_item(self):
        item = reference("r1", "[1]", status="not_found", parsed={"title": "A paper", "doi": "10.9999/none"})
        item["resolution"]["printed_doi_status"] = "unregistered"
        self.assertEqual(assess_consistency(item)["status"], "unresolved_unregistered_doi")
        data = {"schema_version": SCHEMA_VERSION, "scored_references": [item]}
        review = build_review(analyze_artifact(data, CONFIG))
        flagged = [f for f in review["findings"] if f["category"] == "metadata_difference"]
        self.assertEqual(flagged[0]["priority"], "high")


# ---------------------------------------------------------------------------
# Objective: "surfaces possibly missing key literature"
# Extension: "retrieval-augmented verification of suggested citations"
# ---------------------------------------------------------------------------
class FakeTransport:
    """Simulated OpenAlex responses; never reaches the network."""

    def __init__(self, works=None, search=None):
        self.works = works or {}
        self.search = search or []
        self.calls = []

    def _get_json(self, provider, url, params):
        self.calls.append((provider, dict(params)))
        if params.get("filter", "").startswith("openalex:"):
            wanted = params["filter"].split(":", 1)[1].split("|")
            return {"results": [self.works[w] for w in wanted if w in self.works]}
        return {"results": self.search}


class LiteratureGapTests(unittest.TestCase):
    def references(self):
        shared = ["https://openalex.org/W900", "https://openalex.org/W901"]
        return [
            reference("a", "[1]", external={"id": "https://openalex.org/W1"},
                      parsed={"title": "Deep learning for citation auditing", "authors": []},
                      graph={"referenced_works": shared + ["https://openalex.org/W2"]}),
            reference("b", "[2]", external={"id": "https://openalex.org/W2"},
                      graph={"referenced_works": shared + ["https://openalex.org/W1"]}),
            reference("c", "[3]", external={"id": "https://openalex.org/W3"},
                      graph={"referenced_works": ["https://openalex.org/W900"]}),
        ]

    def test_cocitation_counts_support_and_excludes_works_already_cited(self):
        result = cocitation_candidates(self.references(), min_support=2)
        found = {item["openalex_id"]: item for item in result["candidates"]}
        self.assertEqual(found["W900"]["support_count"], 3)
        self.assertEqual(found["W900"]["supporting_references"], ["[1]", "[2]", "[3]"])
        self.assertEqual(found["W901"]["support_count"], 2)
        self.assertNotIn("W1", found)  # cited by the manuscript already
        self.assertNotIn("W2", found)

    def test_candidates_are_resolved_to_real_records(self):
        transport = FakeTransport(works={
            "W900": {"id": "https://openalex.org/W900", "title": "A foundational method", "publication_year": 2009,
                     "doi": "https://doi.org/10.5/found", "cited_by_count": 4100,
                     "authorships": [{"author": {"display_name": "Grace Hopper"}}]},
            # The same work as reference [1], under a different OpenAlex record.
            "W901": {"id": "https://openalex.org/W901", "title": "Deep Learning for Citation Auditing",
                     "publication_year": 2011},
        })
        analyzer = LiteratureGapAnalyzer(SimpleNamespace(GAP_MIN_SUPPORT=2), None, transport=transport)
        result = analyzer.cocitation(self.references())
        titles = [item["title"] for item in result["candidates"]]
        self.assertEqual(titles, ["A foundational method"])  # W901 is reference [1] under another record
        self.assertEqual(result["candidates"][0]["doi"], "10.5/found")
        self.assertEqual(len(transport.calls), 1)  # one batched request

    def test_model_leads_are_verified_or_marked_as_unmatched(self):
        real = {"id": "https://openalex.org/W77", "doi": "https://doi.org/10.7/real",
                "title": "Attention is all you need", "publication_year": 2017,
                "authorships": [{"author": {"display_name": "Ashish Vaswani"}}]}
        transport = FakeTransport(search=[real])
        analyzer = LiteratureGapAnalyzer(SimpleNamespace(), None, transport=transport, clients={})
        verified = analyzer.verify_suggestions(
            [{"title": "Attention is all you need", "authors": "Vaswani, A.; Shazeer, N.", "year": "2017"}], [])
        self.assertEqual(verified[0]["verification_status"], "verified")
        self.assertEqual(verified[0]["matched_record"]["doi"], "10.7/real")
        transport.search = []
        invented = analyzer.verify_suggestions(
            [{"title": "A study that was never written", "authors": "Nobody, N.", "year": "2020"}], [])
        self.assertEqual(invented[0]["verification_status"], "no_matching_record")

    def test_suggestion_text_is_parsed_conservatively(self):
        parsed = parse_suggestion({"title": " T ", "authors": "A. Smith, B. Jones and C. Lee et al.",
                                   "year": "circa 2019"})
        self.assertEqual(parsed["authors"], ["A. Smith", "B. Jones", "C. Lee et al."])
        self.assertEqual(parsed["title"], "T")
        self.assertEqual(parsed["year"], 2019)

    def test_gap_findings_reach_the_review_queue(self):
        data = {"schema_version": SCHEMA_VERSION, "scored_references": self.references(),
                "literature_gap_analysis": {"status": "completed", "cocitation": {"candidates": [
                    {"openalex_id": "W900", "title": "A foundational method", "year": 2009, "support_count": 3,
                     "supporting_references": ["[1]", "[2]", "[3]"]}]},
                    "suggestions": [{"verification_status": "no_matching_record"}], "counts": {}}}
        review = build_review(data)
        categories = [finding["category"] for finding in review["findings"]]
        self.assertIn("possibly_missing_literature", categories)
        self.assertIn("unverified_search_leads", categories)
        self.assertEqual(review["counts"]["possibly_missing_works"], 1)

    def test_offline_mode_never_touches_the_network(self):
        transport = FakeTransport()
        analyzer = LiteratureGapAnalyzer(SimpleNamespace(OFFLINE_MODE=True), None, transport=transport)
        self.assertEqual(analyzer.analyze({"scored_references": self.references()})["status"], "offline_mode")
        self.assertEqual(transport.calls, [])


# ---------------------------------------------------------------------------
# Extension: "improving system efficiency ... caching strategies"
# and the enrichment defects found during the V2.0 audit.
# ---------------------------------------------------------------------------
class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        from database_manager import DatabaseManager
        self.db = DatabaseManager(self.root / "cache.db")
        self.config = SimpleNamespace(
            DATA_DIR=self.root, RESOLUTION_PROVIDERS=("openalex", "crossref", "semantic_scholar"),
            ENABLE_FULL_TEXT_ACQUISITION=False, ENABLE_UNPAYWALL=False, OPENALEX_EMAIL="x",
            RESOLUTION_TIMEOUT_SECONDS=5, RESOLUTION_CANDIDATES_PER_PROVIDER=5,
            ENRICHMENT_CONCURRENCY=4, OFFLINE_MODE=False)

    @staticmethod
    def work(index):
        return {"id": f"https://openalex.org/W{index}", "doi": f"https://doi.org/10.1/{index}",
                "title": f"Paper number {index} about citation audit", "publication_year": 2020,
                "authorships": [{"author": {"display_name": "Anna Meier", "orcid": VALID_ORCID}}],
                "abstract_inverted_index": {"Grounded": [0], "abstract": [1], "text.": [2]},
                "referenced_works": ["https://openalex.org/W900"], "cited_by_count": 3}

    def references(self, count=6):
        return [{"ref_id": f"[{i}]", "reference_uid": f"u{i}", "extraction_status": "located",
                 "parsed": {"title": f"Paper number {i} about citation audit", "authors": ["Meier, A"],
                            "year": 2020, "doi": f"10.1/{i}"}} for i in range(count)]

    def run_enrichment(self, config=None, calls=None):
        from src.enrichers.openalex_enricher import OpenAlexEnricher
        from src.enrichers.phase4_enricher import build_enricher
        from src.evidence.scholarly import ScholarlyClient
        calls = calls if calls is not None else []

        def openalex(enricher, provider, url, params):
            calls.append(provider)
            time.sleep(0.01)
            identifier = (params.get("filter") or "").rsplit("/", 1)[-1]
            return {"results": [self.work(identifier)]} if provider == "openalex" else {"message": {}}

        def scholarly(client, url, params, headers=None):
            calls.append(client.provider)
            return {"message": {"items": []}, "data": []}

        with patch.object(OpenAlexEnricher, "_get_json", openalex), \
                patch.object(ScholarlyClient, "_get_json", scholarly):
            enricher = build_enricher(config or self.config, self.db)
            return enricher, enricher.enrich_references({"metadata": {}, "references_list": self.references()})

    def test_merged_abstracts_survive_multi_provider_resolution(self):
        # Regression: Phase 4 dropped every OpenAlex and Semantic Scholar abstract,
        # leaving resolved references with no source text to assess.
        _, result = self.run_enrichment()
        first = result["enriched_references"][0]
        self.assertEqual(first["resolution"]["status"], "resolved")
        self.assertEqual(first["source_snapshot"]["text"], "Grounded abstract text.")
        self.assertEqual(first["external_metadata"]["graph"]["referenced_works"], ["https://openalex.org/W900"])
        self.assertEqual(first["external_metadata"]["authors"][0]["orcid"], VALID_ORCID)
        self.assertEqual(first["resolution"]["printed_doi_status"], "registered")

    def test_concurrency_preserves_bibliography_order(self):
        _, result = self.run_enrichment()
        self.assertEqual([item["ref_id"] for item in result["enriched_references"]],
                         [f"[{i}]" for i in range(6)])
        self.assertEqual(result["enrichment_metadata"]["concurrency"], 4)

    def test_cache_key_includes_the_provider_set(self):
        # Regression: an OpenAlex-only result could block Crossref/Semantic
        # Scholar for seven days because the cache key ignored the providers.
        self.run_enrichment()
        calls = []
        _, again = self.run_enrichment(calls=calls)
        self.assertEqual(again["enrichment_summary"]["cache_hits"], 6)
        self.assertEqual(calls, [])
        narrower = SimpleNamespace(**{**vars(self.config), "RESOLUTION_PROVIDERS": ("openalex", "crossref")})
        _, changed = self.run_enrichment(config=narrower)
        self.assertEqual(changed["enrichment_summary"]["cache_hits"], 0)

    def test_the_orchestrator_expects_the_version_the_enricher_writes(self):
        # Regression: the reuse check compared against the OpenAlex-only version,
        # so every audit re-ran enrichment and all model scoring.
        from src.enrichers.phase4_enricher import expected_enrichment_version
        enricher, result = self.run_enrichment()
        self.assertEqual(result["enrichment_metadata"]["method_version"], expected_enrichment_version(self.config))
        self.assertEqual(enricher.method_version(), expected_enrichment_version(self.config))

    def test_offline_mode_makes_no_calls_and_says_so(self):
        offline = SimpleNamespace(**{**vars(self.config), "OFFLINE_MODE": True})
        calls = []
        _, result = self.run_enrichment(config=offline, calls=calls)
        self.assertEqual(calls, [])
        self.assertEqual(result["enriched_references"][0]["resolution"]["status"], "offline_mode")
        review = build_review({"schema_version": SCHEMA_VERSION, "scored_references": [
            {**result["enriched_references"][0], "citation_count": 1, "claim_assessments": []}]})
        finding = next(f for f in review["findings"] if f["category"] == "identity_unresolved")
        self.assertEqual(finding["priority"], "informational")
        self.assertFalse(finding["system_issue"])


class RateLimitTests(unittest.TestCase):
    def test_bucket_paces_requests_to_the_configured_rate(self):
        from src.evidence.ratelimit import TokenBucket
        bucket = TokenBucket(rate=20.0, burst=1)
        started = time.monotonic()
        for _ in range(5):
            bucket.acquire()
        self.assertGreaterEqual(time.monotonic() - started, 0.15)


# ---------------------------------------------------------------------------
# Extension: "stronger offline deployment options"
# ---------------------------------------------------------------------------
class OfflineConfigurationTests(unittest.TestCase):
    def errors(self, **overrides):
        from config.settings import Config
        values = {"OFFLINE_MODE": True, "LLM_PROVIDER": "openai_compatible",
                  "ASSESSMENT_PROVIDER": "openai_compatible", "ENABLE_FULL_TEXT_ACQUISITION": False,
                  "SIMILARITY_BACKEND": "lexical", **overrides}
        with patch.multiple(Config, **values):
            return Config.phase4_errors()

    def test_a_fully_local_configuration_is_valid(self):
        self.assertEqual(self.errors(), [])

    def test_offline_mode_rejects_hosted_providers_and_network_features(self):
        self.assertTrue(any("ASSESSMENT_PROVIDER" in e for e in self.errors(ASSESSMENT_PROVIDER="huggingface")))
        self.assertTrue(any("LLM_PROVIDER" in e for e in self.errors(LLM_PROVIDER="google")))
        self.assertTrue(any("full-text" in e for e in self.errors(ENABLE_FULL_TEXT_ACQUISITION=True)))
        self.assertTrue(any("hf_inference" in e for e in self.errors(SIMILARITY_BACKEND="hf_inference")))

    def test_the_local_provider_requires_an_endpoint_and_model(self):
        from src.evidence.providers import JsonProvider, ProviderFailure
        with self.assertRaises(ProviderFailure):
            JsonProvider("openai_compatible", None, "llama3.1", 100, base_url=None)


class AuditOptionTests(unittest.TestCase):
    def test_orcids_are_validated_and_normalized(self):
        from pydantic import ValidationError
        from src.workspace.models import AuditOptions
        options = AuditOptions(author_orcids=[f"https://orcid.org/{VALID_ORCID}", VALID_ORCID])
        self.assertEqual(options.author_orcids, [VALID_ORCID])
        with self.assertRaises(ValidationError):
            AuditOptions(author_orcids=["0000-0002-1825-0098"])


# ---------------------------------------------------------------------------
# Extension: "evaluation on larger multi-domain annotated datasets"
# ---------------------------------------------------------------------------
class EvaluationTests(unittest.TestCase):
    def test_relevance_self_citation_and_domain_metrics_are_reported(self):
        from src.evaluation.dataset import EvaluationDataset, LabeledItem
        from src.evaluation.runner import evaluate_dataset

        def artifact(digest, label_score):
            refs = [reference("r1", "[1]", assessments=[grounded("unrelated")], similarity={"score": label_score},
                              authors=["Meier, A."]),
                    reference("r2", "[2]", assessments=[grounded("supports")], similarity={"score": 80.0})]
            data = {"schema_version": SCHEMA_VERSION, "source_document": {"pdf_sha256": digest},
                    "manuscript_metadata": {"authors": ["Anna Meier"]}, "scored_references": refs}
            return analyze_artifact(data, CONFIG)

        digests = ["a" * 64, "b" * 64, "c" * 64]
        items = []
        for digest, domain in zip(digests, ("biomedicine", "computer science", "computer science")):
            items += [LabeledItem(f"{digest[:4]}-1", digest, "[1]", relevance_label="irrelevant",
                                  is_self_citation=True, domain=domain, annotator="x"),
                      LabeledItem(f"{digest[:4]}-2", digest, "[2]", relevance_label="relevant",
                                  is_self_citation=False, domain=domain, annotator="x")]
        report = evaluate_dataset(EvaluationDataset("t", items),
                                  {digest: artifact(digest, 10.0) for digest in digests})
        aggregate = report["aggregate"]
        self.assertEqual(aggregate["relevance"]["accuracy"], 1.0)
        self.assertEqual(aggregate["self_citation"]["f1"], 1.0)
        self.assertEqual(aggregate["relevance_detector"]["auroc"], 1.0)
        self.assertEqual(set(report["domains"]), {"biomedicine", "computer science"})
        self.assertEqual(report["uncertainty"]["relevance_macro_f1"]["unit"], "manuscript")

        from src.evaluation.report import render_markdown
        markdown = render_markdown(report)
        self.assertIn("\\begin{tabular}", markdown)
        self.assertIn("AUROC", markdown)

    def test_annotation_template_is_blind(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "make_annotation_template", Path(__file__).resolve().parents[1] / "scripts" / "make_annotation_template.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        data = {"source_document": {"pdf_sha256": "d" * 64}, "manuscript_metadata": {"title": "T"},
                "scored_references": [{**reference("r1", "[1]"), "relevance_label": "relevant",
                                       "citation_occurrences": [{"context_window": "As shown [1]."}]}]}
        rows = module.template_rows(data, domain="physics", annotator="a")
        labels = ("identity_label", "support_label", "relevance_label", "metadata_label",
                  "is_self_citation", "extraction_label", "should_be_reviewed")
        self.assertTrue(all(rows[0][label] is None for label in labels))
        for leaked in ("relevance", "RS_final", "claim_assessments", "self_citation"):
            self.assertNotIn(leaked, json.dumps(rows[0]["context"]))
        self.assertEqual(rows[0]["context"]["citing_sentences"], ["As shown [1]."])


class InsightPayloadTests(unittest.TestCase):
    """The React views read these payloads; they must carry the V2.0 analyses."""

    def test_missing_citations_payload_merges_verification_and_cocitation(self):
        from src.webapi.insights import missing_citations
        artifact = {
            "missing_citations": [{"title": "Attention is all you need", "authors": "Vaswani", "year": "2017"}],
            "literature_gap_analysis": {
                "status": "completed",
                "suggestions": [{"id": "candidate-1", "verification_status": "verified",
                                 "matched_record": {"doi": "10.7/real"}, "already_cited": False}],
                "cocitation": {"status": "completed", "references_with_graph": 3, "min_support": 2,
                               "candidates": [{"openalex_id": "W900", "support_count": 3}]},
                "counts": {"verified_suggestions": 1}, "limitations": ["x"]},
        }
        payload = missing_citations(artifact)
        self.assertEqual(payload["items"][0]["verification_status"], "verified")
        self.assertEqual(payload["items"][0]["matched_record"]["doi"], "10.7/real")
        self.assertEqual(payload["cocitation"]["candidates"][0]["openalex_id"], "W900")
        self.assertTrue(payload["available"])

    def test_author_overlap_payload_carries_the_self_citation_analysis(self):
        from src.webapi.insights import author_overlap
        references = [reference(f"s{i}", f"[{i}]", authors=["Meier, A."], citation_count=0) for i in range(3)]
        data = analyze_artifact({"schema_version": SCHEMA_VERSION,
                                 "manuscript_metadata": {"authors": ["Anna Meier"]},
                                 "scored_references": references}, CONFIG)
        payload = author_overlap(data)
        self.assertEqual(payload["self_citation"]["counts"]["self_citations"], 3)
        self.assertEqual(len(payload["self_citation"]["references"]), 3)
        self.assertTrue(payload["self_citation"]["patterns"])


if __name__ == "__main__":
    unittest.main()
