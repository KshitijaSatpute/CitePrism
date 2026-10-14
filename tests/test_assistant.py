"""The plain-language assistant answers only from the audit's own results."""

import json
import unittest
from types import SimpleNamespace

from src.assistant import AuditAssistant, classify_question
from src.evidence import SCHEMA_VERSION
from src.evidence.analysis import analyze_artifact
from src.evidence.review import build_review
from tests.test_v2_requirements import CONFIG, grounded, reference


def sample_review():
    """Eight references covering every kind of result a reader can ask about."""
    references = [
        reference("r1", "[1]", assessments=[grounded("supports")], similarity={"score": 80.0},
                  parsed={"title": "Deep residual learning for image recognition", "authors": ["K. He"]},
                  external={"doi": "10.1/resnet"}),
        reference("r2", "[2]", assessments=[grounded("unrelated")], similarity={"score": 12.0},
                  parsed={"title": "Soil moisture dynamics in alpine meadows", "authors": ["P. Rossi"]}),
        reference("r3", "[3]", assessments=[grounded("context_only")], similarity={"score": 50.0},
                  parsed={"title": "A survey of transfer learning", "authors": ["S. Pan"]}),
        reference("r4", "[4]", authors=["Meier, A."], citation_count=0, assessments=[grounded("supports")],
                  similarity={"score": 75.0}, parsed={"title": "Our earlier detector", "authors": ["A. Meier"]}),
        reference("r5", "[5]", authors=["Meier, A."], assessments=[grounded("supports")], similarity={"score": 70.0},
                  parsed={"title": "Our benchmark", "authors": ["A. Meier"]}),
        reference("r6", "[6]", authors=["Meier, A."], citation_count=0, parsed={"title": "Our workshop paper",
                                                                                 "authors": ["A. Meier"]}),
        reference("r7", "[7]", status="not_found", parsed={"title": "Internal technical report"}),
        reference("r8", "[8]", assessments=[grounded("contradicts")], similarity={"score": 60.0},
                  parsed={"title": "Batch normalization", "authors": ["S. Ioffe"], "year": "2014"},
                  external={"year": 2015, "title": "Batch normalization", "authors": ["S. Ioffe"]}),
    ]
    data = {"schema_version": SCHEMA_VERSION,
            "manuscript_metadata": {"title": "Detecting things", "authors": ["Anna Meier", "Jo Smith"]},
            "scored_references": references}
    config = SimpleNamespace(**vars(CONFIG), SELF_CITATION_RATE_THRESHOLD=0.25, SELF_CITATION_MIN_COUNT=3,
                             SELF_CITATION_CONCENTRATION=0.15)
    return build_review(analyze_artifact(data, config))


class ClassificationTests(unittest.TestCase):
    def test_everyday_questions_reach_the_right_topic(self):
        cases = {
            "Which references are irrelevant?": "irrelevant",
            "are any citations off-topic": "irrelevant",
            "Do the sources back up the claims?": "support",
            "Is there too much self-citation?": "self_citation",
            "did the authors cite their own work a lot": "self_citation",
            "Are there errors in the reference list?": "metadata",
            "is any DOI wrong": "metadata",
            "Is any important work missing?": "missing",
            "Which references couldn't be found?": "unresolved",
            "What should I check first?": "next_steps",
            "How does CitePrism decide?": "method",
            "Give me a summary": "overview",
            "hi": "overview",
        }
        for question, intent in cases.items():
            with self.subTest(question=question):
                self.assertEqual(classify_question(question)[0], intent)

    def test_reference_numbers_are_recognised_in_many_forms(self):
        for question in ("Explain reference 12", "what about [12]?", "ref #12", "tell me about #12"):
            self.assertEqual(classify_question(question), ("reference", "12"))


class AnswerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.review = sample_review()
        cls.assistant = AuditAssistant(cls.review)

    def markers(self, answer):
        return [item["marker"] for item in answer["references"]]

    def test_summary_counts_match_the_review(self):
        answer = self.assistant.summary()
        self.assertIn("8 references", answer["text"])
        attention = self.review["counts"]["attention_references"]
        self.assertIn(f"{attention} references are worth a closer look", answer["text"])
        self.assertTrue(answer["suggestions"])
        self.assertEqual(answer["grounding"], "audit_results")

    def test_irrelevant_lists_flagged_before_borderline(self):
        answer = self.assistant.answer("Which references may be irrelevant?")
        markers = self.markers(answer)
        self.assertEqual(markers[0], "[2]")
        self.assertIn("[3]", markers)
        self.assertNotIn("[1]", markers)

    def test_support_names_the_contradiction(self):
        answer = self.assistant.answer("Do the sources back up the claims?")
        self.assertEqual(answer["references"][0]["marker"], "[8]")
        self.assertEqual(answer["references"][0]["tone"], "warning")

    def test_self_citation_reports_rate_and_pattern(self):
        answer = self.assistant.answer("Is there too much self-citation?")
        self.assertIn("3 references", answer["text"])
        self.assertIn("38%", answer["text"])
        self.assertTrue(any("share an author" in point for point in answer["points"]))

    def test_metadata_mentions_the_year_mismatch(self):
        answer = self.assistant.answer("Are there errors in the reference list?")
        self.assertIn("[8]", self.markers(answer))

    def test_unresolved_lists_only_unmatched_references(self):
        answer = self.assistant.answer("Which references couldn't be found?")
        self.assertEqual(self.markers(answer), ["[7]"])

    def test_explain_a_single_reference(self):
        answer = self.assistant.answer("Explain reference 2")
        self.assertIn("Soil moisture", answer["text"])
        self.assertTrue(any("different things" in point for point in answer["points"]))

    def test_unknown_reference_number_is_said_plainly(self):
        self.assertIn("could not find reference 99", self.assistant.answer("explain reference 99")["text"])

    def test_title_words_find_a_reference(self):
        answer = self.assistant.answer("what is wrong with the soil moisture alpine meadows one")
        self.assertIn("[2]", self.markers(answer))

    def test_unanswerable_question_without_a_model_offers_help(self):
        answer = self.assistant.answer("What is the capital of France?")
        self.assertEqual(answer["intent"], "help")
        self.assertGreaterEqual(len(answer["suggestions"]), 4)

    def test_answers_never_use_verdict_language(self):
        questions = ["summary", "irrelevant", "self citation", "errors in the reference list", "missing",
                     "what should I check first", "how does it work", "explain reference 8"]
        for question in questions:
            text = json.dumps(self.assistant.answer(question)).lower()
            for word in ("misconduct", "fraud", "plagiar", "cheat"):
                self.assertNotIn(word, text, question)


class IncompleteAuditTests(unittest.TestCase):
    def test_failed_checks_are_never_summarised_as_clean(self):
        data = {"schema_version": SCHEMA_VERSION, "scored_references": [reference(
            "r1", "[1]", assessments=[{"status": "assessment_unavailable", "error_code": "timeout"}])]}
        data["scored_references"][0]["evidence_status"] = "assessment_unavailable"
        summary = AuditAssistant(build_review(analyze_artifact(data, CONFIG))).summary()
        self.assertIn("incomplete", summary["text"])
        self.assertNotIn("nothing stands out as needing attention", summary["text"])


class ModelFallbackTests(unittest.TestCase):
    def setUp(self):
        self.review = sample_review()

    def test_model_answers_keep_only_real_references(self):
        def model(prompt):
            self.assertIn("AUDIT FACTS", prompt)
            return json.dumps({"can_answer": True, "answer": "Reference [2] is about soil, not vision.",
                               "reference_markers": ["[2]", "[99]"]})
        answer = AuditAssistant(self.review, model=model, model_name="test-model").answer(
            "Why would a vision paper cite a soil study?")
        self.assertEqual(answer["grounding"], "model_phrased")
        self.assertEqual([item["marker"] for item in answer["references"]], ["[2]"])
        self.assertEqual(answer["model"], "test-model")

    def test_model_that_cannot_answer_falls_back_to_help(self):
        model = lambda prompt: json.dumps({"can_answer": False, "answer": ""})
        self.assertEqual(AuditAssistant(self.review, model=model).answer("Who funded this?")["intent"], "help")

    def test_model_failure_never_breaks_the_assistant(self):
        def model(prompt):
            raise RuntimeError("503")
        self.assertEqual(AuditAssistant(self.review, model=model).answer("Who funded this?")["intent"], "help")

    def test_built_in_topics_never_call_the_model(self):
        def model(prompt):
            raise AssertionError("model must not be called")
        AuditAssistant(self.review, model=model).answer("Which references may be irrelevant?")


if __name__ == "__main__":
    unittest.main()
