"""5.4.2: what a real report on a bachelor's engineering paper still got wrong.

The audit was precise about the paper but left four checks unfinished (dblp failed three
times, one model reply came back in the wrong shape), called two genuine co-authors a
difference, and showed PDF spacing artifacts and clipped explanations in the web app.
"""

import json
import unittest
from types import SimpleNamespace

from src.evidence.consistency import compare, generic_venue
from src.evidence.providers import CallRecorder, ProviderFailure
from src.evidence.resolution import resolve_with_providers
from src.evidence.review import build_review, describe_differences, not_found_detail
from src.evidence.scholarly import DblpClient, query_title
from src.evidence.text import repaired_title
from src.scorers.relevance_scorer import normalized_row
from tests.test_coverage_providers import DBLP_RESPONSE, FakeResponse, StubClient


class Session:
    """dblp.org fails; the Trier mirror answers."""

    def __init__(self, failing=("dblp.org",)):
        self.failing, self.calls = failing, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if any(f"//{host}/" in url for host in self.failing):
            return FakeResponse(500, {})
        return FakeResponse(200, DBLP_RESPONSE)


PARSED = {"title": "Experimental Demonstration Of Underwater Optical Wireless Communication",
          "authors": ["Kashan Iqbal"], "year": "2022"}


class SpecialistOutageTests(unittest.TestCase):
    def test_a_dblp_outage_leaves_the_search_incomplete_not_unfinished(self):
        resolution = resolve_with_providers(PARSED, {"crossref": StubClient([])},
                                            supplementary={"dblp": StubClient(error="provider_error"),
                                                           "europe_pmc": StubClient([])})
        self.assertEqual(resolution["status"], "not_found")
        self.assertEqual(resolution["search_incomplete"], ["dblp"])
        detail = not_found_detail(resolution, "not_found")
        self.assertIn("No matching work was found in Crossref and Europe PMC.", detail)   # OpenAlex was not asked
        self.assertIn("dblp returned an error, so that index was not searched", detail)

    def test_a_general_index_outage_still_withholds_the_verdict(self):
        resolution = resolve_with_providers(PARSED, {"crossref": StubClient(error="service_unavailable")},
                                            supplementary={"dblp": StubClient([])})
        self.assertEqual(resolution["status"], "provider_unavailable")
        self.assertNotIn("search_incomplete", resolution)

    def test_the_http_status_is_recorded_and_named(self):
        class Failing:
            def search(self, _parsed):
                raise ProviderFailure("provider_error", "dblp", status_code=500)
        resolution = resolve_with_providers(PARSED, {"crossref": StubClient([])}, supplementary={"dblp": Failing()})
        self.assertEqual(resolution["provider_errors"][0]["http_status"], 500)
        self.assertIn("dblp returned an error (HTTP 500)", not_found_detail(resolution, "not_found"))

    def test_the_dblp_mirror_answers_when_dblp_org_does_not(self):
        session = Session()
        client = DblpClient(SimpleNamespace(RESOLUTION_CANDIDATES_PER_PROVIDER=5, RESOLUTION_TIMEOUT_SECONDS=5),
                            CallRecorder(None, max_attempts=1, retry_delay=0), session)
        results = client.search({"title": "Deep Residual Learning for Image Recognition"})
        self.assertEqual(results[0]["doi"], "10.1109/cvpr.2016.90")
        self.assertEqual([url.split("/search")[0] for url, _ in session.calls],
                         ["https://dblp.org", "https://dblp.uni-trier.de"])

    def test_both_dblp_hosts_failing_is_still_an_outage(self):
        client = DblpClient(SimpleNamespace(), CallRecorder(None, max_attempts=1, retry_delay=0),
                            Session(failing=("dblp.org", "dblp.uni-trier.de")))
        with self.assertRaises(ProviderFailure):
            client.search({"title": "Anything"})

    def test_search_queries_rejoin_pdf_spaced_hyphens(self):
        self.assertEqual(query_title({"title": "Li -Fi Based Underwater Communication"}),
                         "Li-Fi Based Underwater Communication")
        self.assertEqual(query_title({"title": "Deep learning - a review"}), "Deep learning - a review")
        session = Session(failing=())
        DblpClient(SimpleNamespace(), CallRecorder(None, max_attempts=1, retry_delay=0), session).search(
            {"title": "Real -Time Audio Transmission"})
        self.assertEqual(session.calls[0][1]["q"], "Real-Time Audio Transmission")


class AssessmentRetryTests(unittest.TestCase):
    def scorer(self, provider):
        import tempfile
        from pathlib import Path
        from tests.evidence_fixtures import scored
        return scored(Path(tempfile.mkdtemp()), provider)

    def test_a_reply_in_the_wrong_shape_is_asked_again_once_on_its_own(self):
        from tests.evidence_fixtures import GroundedProvider

        class FirstReplyBroken(GroundedProvider):
            calls = []

            def judge_batch(self, tasks):
                self.calls.append(len(tasks))
                rows = super().judge_batch(tasks)
                if len(self.calls) == 1:
                    rows[0]["claim_quote"] = None           # the shape Gemini returned for [4]
                return rows

        provider = FirstReplyBroken()
        data = self.scorer(provider)
        assessments = [a for r in data["scored_references"] for a in r["claim_assessments"]]
        self.assertTrue(all(a["status"] == "grounded_proposal" for a in assessments))
        retried = [a for a in assessments if a.get("first_attempt_error")]
        self.assertEqual([a["first_attempt_error"] for a in retried], ["invalid_response_schema"])
        self.assertEqual(provider.calls[1:], [1])

    def test_failed_quotations_are_never_retried(self):
        from tests.evidence_fixtures import GroundedProvider

        class Invented(GroundedProvider):
            calls = []

            def judge_batch(self, tasks):
                self.calls.append(len(tasks))
                rows = super().judge_batch(tasks)
                for row in rows:
                    row["decision"], row["source_quotes"] = "supports", ["A sentence no abstract contains at all."]
                return rows

        provider = Invented()
        data = self.scorer(provider)
        self.assertEqual(len(provider.calls), 1)
        codes = {a.get("error_code") for r in data["scored_references"] for a in r["claim_assessments"]}
        self.assertEqual(codes, {"quote_not_in_own_source"})

    def test_a_second_wrong_shape_keeps_the_first_error(self):
        from tests.evidence_fixtures import GroundedProvider

        class AlwaysBroken(GroundedProvider):
            def judge_batch(self, tasks):
                rows = super().judge_batch(tasks)
                for row in rows:
                    row["rationale"] = None
                return rows

        data = self.scorer(AlwaysBroken())
        assessments = [a for r in data["scored_references"] for a in r["claim_assessments"]]
        self.assertTrue(assessments)
        self.assertTrue(all(a["error_code"] == "invalid_response_schema" for a in assessments))
        self.assertTrue(all(a["retry_error_code"] == "invalid_response_schema" for a in assessments))

    def test_only_representational_variants_are_normalized(self):
        row = {"task_id": "t", "decision": " Context-only ", "source_quotes": "One quote.", "claim_quote": "c",
               "rationale": "r"}
        normalized = normalized_row(row)
        self.assertEqual((normalized["decision"], normalized["source_quotes"]), ("context_only", ["One quote."]))
        self.assertEqual(row["decision"], " Context-only ")                        # the observation is untouched
        self.assertEqual(normalized_row({"decision": "agrees"})["decision"], "agrees")  # not a decision: stays invalid
        self.assertEqual(normalized_row({"decision": "unrelated", "source_quotes": None})["source_quotes"], [])
        self.assertIsNone(normalized_row({"decision": "supports", "source_quotes": None})["source_quotes"])


class AuthorAndVenueTests(unittest.TestCase):
    PRINTED = {"title": "Real-Time Audio Transmission Using Visible Light Communication", "year": "2018",
               "venue": "IEEE Conference",
               "authors": ["Soh, M. Y.", "Ng, W. X.", "Zou, Q.", "Lee, D.", "Teo, T. H.", "Yeo, K. S."]}
    SOURCE = {"title": "Real-Time Audio Transmission Using Visible Light Communication", "year": 2018,
              "venue": "TENCON 2018 - 2018 IEEE Region 10 Conference",
              "authors": [{"display_name": "Mei Yu Soh"}, {"display_name": "Qiong Zou"},
                          {"display_name": "Denise Lee"}, {"display_name": "T. Hui Teo"}]}

    def test_a_shorter_author_record_is_a_note_not_a_difference(self):
        differences = {d["field"]: d for d in compare(self.PRINTED, self.SOURCE)}
        self.assertEqual(differences["authors"]["severity"], "info")
        self.assertIn("lists 4 of the 6 printed authors; it leaves out Ng, W. X. and Yeo, K. S. Every",
                      differences["authors"]["detail"])

    def test_a_printed_name_the_record_does_not_have_is_still_a_difference(self):
        source = dict(self.SOURCE, authors=self.SOURCE["authors"] + [{"display_name": "Wei Lim"},
                                                                     {"display_name": "Ann Koh"}])
        differences = {d["field"]: d for d in compare(self.PRINTED, source)}
        self.assertEqual(differences["authors"]["severity"], "minor")

    def test_a_generic_venue_is_named_as_such(self):
        differences = {d["field"]: d for d in compare(self.PRINTED, self.SOURCE)}
        self.assertEqual(differences["venue"]["severity"], "minor")
        self.assertIn("generic", differences["venue"]["detail"])
        for venue, generic in [("IEEE Conference", True), ("Journal", True), ("Springer", False), ("IEEE", False),
                               ("IEEE Access", False), ("Proceedings of the IEEE", False),
                               ("International Journal of Research Publication and Reviews", False)]:
            with self.subTest(venue=venue):
                self.assertEqual(generic_venue(venue), generic)

    def test_differences_read_as_separate_sentences(self):
        text = describe_differences([
            {"field": "year", "as_cited": "2016", "candidate": "2012", "severity": "major",
             "detail": "The years differ by 4."},
            {"field": "venue", "as_cited": "IEEE Conference", "candidate": "Photonics Global Conference",
             "severity": "minor", "detail": "The printed venue does not match the source record."}])
        self.assertNotIn(".;", text)
        self.assertIn("— the years differ by 4. Venue: printed “IEEE Conference”", text)
        from src.assistant.engine import AuditAssistant
        parts = [p for p in AuditAssistant.DIFFERENCE_START.split(text) if p.strip()]
        self.assertEqual([p.split(":")[0] for p in parts], ["Year", "Venue"])


class TitleTests(unittest.TestCase):
    SOURCE = ("Experimental Demonstration of 55-m / 2-Gbps Underwater Wireless Optical Communication Using SiPM "
              "Diversity Reception and Nonlinear Decision-Feedback Equalizer")

    def test_text_layer_artifacts_are_repaired_for_display_only(self):
        printed = ("Experimental Demonstration Of 2 -Gbps Underwater Wireless Optical Communication Using SiPM "
                   "Diversity Reception And Nonlinear Decision Feedback Equa lizer.")
        self.assertEqual(repaired_title(printed, self.SOURCE),
                         "Experimental Demonstration Of 2-Gbps Underwater Wireless Optical Communication Using SiPM "
                         "Diversity Reception And Nonlinear Decision Feedback Equalizer.")
        self.assertEqual(repaired_title("Li -Fi Based Underwater Communication"), "Li-Fi Based Underwater Communication")
        self.assertEqual(repaired_title("Lehr- und Lernforschung"), "Lehr- und Lernforschung")
        # Two real words stay two words when the source has them apart.
        self.assertEqual(repaired_title("A data set", "A data set for testing"), "A data set")


def janvi_review(dblp_failed=True):
    """The paper as a 5.4.2 re-run records it."""
    from src.evidence import SCHEMA_VERSION
    from src.evidence.analysis import analyze_artifact
    from tests.test_v2_requirements import CONFIG, reference

    searched = ["crossref", "datacite", "dblp", "europe_pmc", "openalex", "semantic_scholar"]

    def ref(n, title, authors, year, cited, status="resolved", external=None, issues=None, venue=None,
            assessments=None, similarity=None, resolution=None, evidence=None):
        item = reference(f"r{n}", f"[{n}]", citation_count=cited, status=status, external=external,
                         parsed={"title": title, "authors": authors, "year": year, "venue": venue},
                         assessments=assessments, similarity=similarity)
        item["resolution"].update(resolution or {})
        if evidence:
            item["evidence_status"] = evidence
        if issues:
            item["original_data"]["extraction_status"] = "needs_review"
            item["original_data"]["extraction_issues"] = issues
        return item

    def grounded(decision):
        return {"status": "grounded_proposal", "decision": decision, "validated_quotes": [{"text": "q"}]}

    incomplete = ({"search_incomplete": ["dblp"],
                   "provider_errors": [{"provider": "dblp", "error_code": "provider_error", "http_status": 500}]}
                  if dblp_failed else {})
    refs = [
        ref(1, "Li -Fi Based Underwater Communication", ["Srikanth, B."], "2024", 1, status="not_found",
            resolution={"providers_queried": searched}, evidence="identity_unresolved"),
        ref(3, "5 Mbps Optical Wireless Communication", ["Simpson, J. A."], "2018", 1, venue="IEEE Conference",
            external={"title": "5 Mbps optical wireless communication", "authors": ["Jim A. Simpson"], "year": 2010,
                      "venue": "OCEANS 2010 MTS/IEEE SEATTLE"},
            assessments=[grounded("supports")], similarity={"score": 72.0}),
        ref(5, "Underwater Communication System for Deep Sea Divers", ["Das, A. K."], "2016", 1,
            external={"title": "Underwater communication system for deep sea divers", "authors": ["Ankita Das"],
                      "year": 2012}, assessments=[grounded("insufficient_evidence")], similarity={"score": 80.0}),
        ref(7, "A survey", ["Fang, C."], "2023", 0, external={"title": "A survey", "authors": ["Chengwei Fang"],
                                                             "year": 2023}),
        ref(8, None, ["Abdelrahman Elfikky"], None, 0, status="not_found",
            issues=["bibliography_fragment", "bibliography_boundary_ambiguous"]),
        ref(9, "Underwater Visible Light Communication", ["Aly"], "2024", 1, status="unresolved",
            issues=["bibliography_boundary_ambiguous"], evidence="identity_unresolved"),
        ref(10, "Experimental Demonstration Of Underwater Optical Wireless Communication", ["Kashan Iqbal"], "2022",
            0, status="not_found", resolution={"providers_queried": searched, **incomplete}),
    ]
    data = {"schema_version": SCHEMA_VERSION,
            "manuscript_metadata": {"title": "UNDERWATER COMMUNICATION SYSTEM FOR SCUBA DIVERS", "authors": ["A B"]},
            "extraction_metadata": {"bibliography_ambiguities": [
                {"kind": "possible_split_entry", "reference_ids": ["[8]", "[9]"]}]},
            "scored_references": refs}
    config = SimpleNamespace(**vars(CONFIG), SELF_CITATION_RATE_THRESHOLD=0.25, SELF_CITATION_MIN_COUNT=3,
                             SELF_CITATION_CONCENTRATION=0.15)
    return build_review(analyze_artifact(data, config))


class ReportTests(unittest.TestCase):
    def test_an_incomplete_search_is_a_finding_with_its_caveat_not_a_technical_item(self):
        review = janvi_review()
        r10 = [f for f in review["findings"] if f["reference_id"] == "r10" and f["category"] == "identity_unresolved"]
        self.assertEqual(len(r10), 1)
        self.assertFalse(r10[0]["system_issue"])
        self.assertEqual(r10[0]["summary"], "Not found in scholarly databases")
        self.assertIn("dblp returned an error (HTTP 500), so that index was not searched", r10[0]["explanation"])
        self.assertEqual(review["counts"]["system_issues"], 0)

    def test_the_entry_after_a_split_points_back_to_it(self):
        review = janvi_review()
        r10 = next(f for f in review["findings"] if f["reference_id"] == "r10" and f["category"] == "not_cited")
        self.assertIn("the text's [9] probably cites this entry, and it is cited after all", r10["explanation"])
        from src.assistant.engine import AuditAssistant
        assistant = AuditAssistant(review)
        self.assertTrue(assistant._detail(r10, assistant_opts(), full=True).startswith("Entries [8] and [9]"))
        answer = assistant.answer("which references could not be found?")
        notes = {item["marker"]: item["note"] for item in answer["references"]}
        self.assertIn("Half of one reference split in two ([8] and [9])", notes["[9]"])
        self.assertIn("not every database answered", notes["[10]"])

    def test_the_summary_says_what_the_support_check_covered_in_one_line(self):
        from src.assistant.engine import AuditAssistant
        points = AuditAssistant(janvi_review()).answer("summary")["points"]
        coverage = [p for p in points if p.startswith("Of the ")]
        self.assertEqual(coverage, [
            "Of the 4 cited references, 2 could be checked against the work they cite: 1 looks clearly relevant to "
            "the sentences citing it and 1 does not clearly back up its claim. The other 2 could not be checked: "
            "1 was not found in the databases, 1 is half of the split entry."])
        self.assertFalse(any("scored references look clearly relevant" in p for p in points))
        simple = AuditAssistant(janvi_review()).answer("explain it like I'm 5")["points"]
        self.assertTrue(any(p.startswith("I checked 2 of the 4 sources the text cites") for p in simple))

    def test_review_titles_are_repaired_for_the_web_app(self):
        titles = {r["marker"]: r["title"] for r in janvi_review()["references"]}
        self.assertEqual(titles["[1]"], "Li-Fi Based Underwater Communication")


def assistant_opts():
    from src.assistant.engine import Opts
    return Opts()


if __name__ == "__main__":
    unittest.main()


class IndependentReviewTests(unittest.TestCase):
    """Defects an independent review found in the first 5.4.2 commit."""

    def test_a_403_from_dblp_tries_the_mirror_and_is_not_called_a_key_problem(self):
        class Blocked(Session):
            def get(self, url, params=None, headers=None, timeout=None):
                self.calls.append((url, dict(params or {})))
                return FakeResponse(403, {}) if "//dblp.org/" in url else FakeResponse(200, DBLP_RESPONSE)
        session = Blocked()
        client = DblpClient(SimpleNamespace(), CallRecorder(None, max_attempts=1, retry_delay=0), session)
        self.assertTrue(client.search({"title": "Deep Residual Learning for Image Recognition"}))
        self.assertEqual(len(session.calls), 2)
        detail = not_found_detail({"providers_queried": ["crossref", "dblp"], "search_incomplete": ["dblp"],
                                   "provider_errors": [{"provider": "dblp", "error_code": "authentication",
                                                        "http_status": 403}]}, "not_found")
        self.assertIn("dblp refused the request (a firewall, security software or proxy may be blocking it)", detail)
        self.assertNotIn("API key", detail)
        self.assertIn("see “Database errors” in the README", detail)            # re-running will not cure a block

    def test_unchecked_references_are_given_their_real_reason(self):
        from src.assistant.engine import AuditAssistant
        refs = [{"id": f"r{n}", "marker": f"[{n}]", "title": "T", "citation_count": 1, "identity_status": status,
                 "evidence_status": "identity_unresolved", "relevance_label": "not_scored"}
                for n, status in enumerate(["provider_unavailable", "provider_unavailable", "offline_mode", "not_found"])]
        review = {"title": "P", "references": refs, "findings": [], "counts": {}}
        line = AuditAssistant(review)._coverage(refs, simple=False)
        self.assertIn("for 2 a database did not respond", line)
        self.assertIn("1 was not looked up (offline mode)", line)
        self.assertIn("1 was not found in the databases", line)
        self.assertNotIn("3 were not found", line)

    def test_abbreviated_names_of_real_journals_are_not_generic(self):
        base, record = {"title": "T", "authors": ["Smith, J."]}, {"title": "T", "authors": ["John Smith"]}
        for printed, source in [("Proc. IEEE", "Proceedings of the IEEE"), ("Proceedings of IEEE", "Proceedings of the IEEE"),
                                ("Journal of ACM", "Journal of the ACM")]:
            with self.subTest(printed=printed):
                fields = [d["field"] for d in compare(dict(base, venue=printed), dict(record, venue=source))]
                self.assertNotIn("venue", fields)
        venue = [d for d in compare(dict(base, venue="IEEE Transactions"),
                                    dict(record, venue="IEEE Transactions on Communications")) if d["field"] == "venue"]
        self.assertIn("generic", venue[0]["detail"])

    def test_a_datacite_outage_still_asks_doi_org_about_a_printed_doi(self):
        class Registry:
            calls = 0

            def registered(self, doi):
                Registry.calls += 1
                return False
        resolution = resolve_with_providers(dict(PARSED, doi="10.1234/fake.123"), {"crossref": StubClient([])},
                                            supplementary={"datacite": StubClient(error="service_unavailable")},
                                            registry=Registry())
        self.assertEqual(Registry.calls, 1)
        self.assertEqual(resolution["printed_doi_status"], "unregistered")

    def test_coverage_grammar_agrees_with_every_count(self):
        from src.assistant.engine import AuditAssistant
        refs = [{"id": f"r{n}", "marker": f"[{n}]", "title": "T", "citation_count": 1, "identity_status": "resolved",
                 "evidence_status": "grounded_proposal", "relevance_label": "topic_only"} for n in range(2)]
        refs.append({"id": "r9", "marker": "[9]", "title": "T", "citation_count": 1, "identity_status": "legacy_unverified",
                     "evidence_status": "legacy_unverified"})
        findings = [{"id": f"f{n}", "reference_id": f"r{n}", "category": "limited_support", "priority": "low",
                     "summary": "s", "explanation": "e"} for n in range(2)]
        review = {"title": "P", "references": refs, "findings": findings, "counts": {}}
        standard = AuditAssistant(review)._coverage(refs, simple=False)
        simple = AuditAssistant(review)._coverage(refs, simple=True)
        self.assertIn("2 do not clearly back up their claims", standard)
        self.assertIn("1 comes from an older audit", standard)
        self.assertIn("2 don't clearly say what the paper claims", simple)

    def test_several_failed_indexes_are_named_in_the_plural(self):
        detail = not_found_detail({"providers_queried": ["crossref", "dblp", "europe_pmc"],
                                   "search_incomplete": ["dblp", "europe_pmc"],
                                   "provider_errors": [{"provider": "dblp", "error_code": "provider_error"},
                                                       {"provider": "europe_pmc", "error_code": "timeout"}]}, "not_found")
        self.assertIn("so those indexes were not searched", detail)

    def test_openalex_is_named_only_when_it_was_asked(self):
        resolution = resolve_with_providers(PARSED, {"crossref": StubClient([])})
        self.assertNotIn("openalex", resolution["provider_candidate_counts"])
        self.assertEqual(not_found_detail(resolution, "not_found").split(".")[0], "No matching work was found in Crossref")
        asked = resolve_with_providers(PARSED, {"crossref": StubClient([])}, openalex_candidates=[])
        self.assertIn("openalex", asked["provider_candidate_counts"])

    def test_a_minus_sign_is_not_glued_to_the_word_before_it(self):
        self.assertEqual(repaired_title("Samples stored at -80 °C from -40 to -20"), "Samples stored at -80 °C from -40 to -20")
        self.assertEqual(query_title({"title": "Stored at -80 °C"}), "Stored at -80 °C")
        self.assertEqual(repaired_title("2 -Gbps Li -Fi"), "2-Gbps Li-Fi")

    def test_the_shorter_record_note_needs_the_same_first_author_and_two_record_authors(self):
        first_differs = compare({"title": "T", "authors": ["Smith, J.", "Doe, A.", "Lee, B."]},
                                {"title": "T", "authors": ["Anna Doe", "Bo Lee"]})
        self.assertEqual({d["field"]: d["severity"] for d in first_differs}, {"first_author": "major", "authors": "minor"})
        one_author = compare({"title": "T", "authors": ["Smith, J.", "Doe, A."]}, {"title": "T", "authors": ["John Smith"]})
        self.assertEqual([(d["field"], d["severity"]) for d in one_author], [("authors", "minor")])

    def test_a_title_containing_a_field_label_is_not_split(self):
        from src.assistant.engine import AuditAssistant
        text = ("Title: printed “What changed? Year: 2020 in review”, the database record has “What changed”. "
                "Year: printed “2018”, the database record has “2010” — the years differ by 8.")
        parts = AuditAssistant._split_differences(text)
        self.assertEqual(len(parts), 2)
        self.assertTrue(parts[1].startswith("Year: printed “2018”"))

    def test_diagnostics_from_a_retry_are_kept(self):
        import tempfile
        from pathlib import Path
        from tests.evidence_fixtures import GroundedProvider, scored

        class StrayOnRetry(GroundedProvider):
            calls = 0

            def judge_batch(self, tasks):
                StrayOnRetry.calls += 1
                rows = super().judge_batch(tasks)
                if StrayOnRetry.calls == 1:
                    rows[0]["claim_quote"] = None
                else:
                    rows.append(dict(rows[0], task_id="stray-task"))
                return rows

        data = scored(Path(tempfile.mkdtemp()), StrayOnRetry())
        codes = [d["code"] for d in data["scoring_metadata"]["diagnostics"]]
        self.assertIn("unknown_task_id", codes)
