"""Defects found by an independent review of 5.4 (each reproduced, then fixed).

High: numbered lists lost entries after "1. Aufl." or "(1) 23-45" lines; the
degree rule removed the surname "Ma"; DOIs broken after "." were truncated.
Medium/low: context lost on "explain it more simply"; counts that ignored
decisions; a spelling corrector that changed meaning; "errors" routed to the
technical topic; "nothing missing" claimed when the check did not finish;
ways around local_only; needless full re-runs; weak count checks; faithful
German translations rejected; an umlaut taken for German; accusation phrasings
missed; repeated benchmark trials; a quadratic PDF reader.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.assistant import AuditAssistant, guard
from src.assistant.understanding import correct_spelling, detect_language, understand
from src.evidence.bibliography import find_doi
from src.evidence.extraction import numbered_slots, pdf_source
from src.evidence.identity import resolve_candidates
from src.evidence.self_citation import person_key
from src.workspace.data_policy import data_handling, is_local_url, policy_errors
from tests.test_assistant import sample_review
from tests.test_conversation import Conversation, everything

FIXTURES = Path(__file__).parent / "fixtures" / "evidence"


class NumberedListTests(unittest.TestCase):
    def numbers(self, lines):
        return [m.group(1) for m in numbered_slots("\n".join(["References", *lines]), 0)]

    def test_an_edition_line_does_not_restart_the_list(self):
        lines = []
        for n in range(1, 31):
            lines.append(f"[{n}] Author{n}, A. Title {n}. Publisher.")
            if n == 10:
                lines.append("1. Aufl. 2019.")
        self.assertEqual(self.numbers(lines), [f"[{n}]" for n in range(1, 31)])

    def test_wrapped_issue_numbers_do_not_restart_the_list(self):
        lines = [item for n in range(1, 31) for item in (f"{n}. Author (2019). Title {n}. Journal, 12", "(1) 23-45.")]
        self.assertEqual(self.numbers(lines), [f"{n}." for n in range(1, 31)])

    def test_out_of_order_extraction_keeps_every_entry(self):
        order = list(range(1, 11)) + list(range(21, 31)) + list(range(11, 21))
        self.assertEqual(len(self.numbers([f"[{n}] A{n}. Title." for n in order])), 30)

    def test_years_and_two_lists(self):
        self.assertEqual(self.numbers([f"{n}. A (2016). T.\n2016. Reprint" for n in range(1, 6)]),
                         [f"{n}." for n in range(1, 6)])
        two = [f"[{n}] A." for n in range(1, 6)] + ["Further"] + [f"[{n}] B." for n in range(1, 6)]
        self.assertEqual(len(self.numbers(two)), 10)


class NameTests(unittest.TestCase):
    def test_short_surnames_are_not_degrees(self):
        cases = {"Yi Ma": ("ma", "y"), "YI MA": ("ma", "y"), "X. Ma": ("ma", "x"), "Jean Mba": ("mba", "j"),
                 "Md Rahman": ("rahman", "m"), "Rahman, Md": ("rahman", "m"), "Ma, Yi": ("ma", "y")}
        for name, key in cases.items():
            with self.subTest(name=name):
                self.assertEqual(person_key(name), key)

    def test_degrees_are_still_removed(self):
        for name in ("John Smith PhD", "John Smith, MA", "Smith, John, MD", "John Smith MBA", "Smith, John, Ph.D."):
            self.assertEqual(person_key(name), ("smith", "j"), name)
        self.assertEqual(person_key("Anna Meier M.A."), ("meier", "a"))

    def test_a_short_surname_resolves(self):
        record = {"id": "w", "title": "Deep learning for vision", "authors": [{"display_name": "Yi Ma"}],
                  "publication_year": 2020}
        query = {"title": "Deep learning for vision", "authors": ["Ma, Yi"], "year": 2020}
        self.assertEqual(resolve_candidates([record], query)["status"], "resolved")


class DoiTests(unittest.TestCase):
    def test_dois_broken_by_the_pdf_are_rejoined(self):
        cases = {
            "doi:10.1016/j. jss.2020.110": "10.1016/j.jss.2020.110",
            "https://doi.org/10.1080/0309877X. 2019.1234567": "10.1080/0309877x.2019.1234567",
            "https://doi.org/10.1007/ 978-3-658-44153-1_3": "10.1007/978-3-658-44153-1_3",
            "https://doi.org/10. 1007/s11192-020-03": "10.1007/s11192-020-03",
            "doi:10.1002/(SICI)1097-4571(199806)49:8<693::AID-ASI4>3.0.CO;2-0":
                "10.1002/(sici)1097-4571(199806)49:8<693::aid-asi4>3.0.co;2-0",
        }
        for text, doi in cases.items():
            with self.subTest(text=text):
                self.assertEqual(find_doi("Smith, J. (2020). Title. " + text), doi)

    def test_following_words_are_never_glued_on(self):
        for text, doi in {"doi:10.1000/xyz123. Retrieved from http://x": "10.1000/xyz123",
                          "doi:10.1000/xyz123. in press": "10.1000/xyz123",
                          "doi: 10.1007/978-3-658-44153-1_3 [Zugriff: 12.03.2021]": "10.1007/978-3-658-44153-1_3",
                          "doi:10.1016/S0140-6736(20)30183-5. PMID: 123": "10.1016/s0140-6736(20)30183-5"}.items():
            self.assertEqual(find_doi(text), doi, text)


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.assistant = AuditAssistant(sample_review())

    def test_explain_it_more_simply_keeps_the_topic(self):
        chat = Conversation(self.assistant)
        chat.ask("explain reference 8")
        simpler = chat.ask("Explain it more simply")
        self.assertEqual((simpler["intent"], simpler["register"]), ("reference", "simple"))
        self.assertEqual(simpler["reference_ids"], [r["id"] for r in sample_review()["references"]
                                                    if r["marker"] == "[8]"])

    def test_explain_it_more_simply_after_a_several_part_answer(self):
        chat = Conversation(self.assistant)
        first = chat.ask("which references may be irrelevant and is there too much self-citation?")
        self.assertEqual(first["topics"], ["irrelevant", "self_citation"])
        chat.history[-1]["topics"] = first["topics"]
        simpler = chat.ask("Explain it more simply")
        self.assertEqual([s["intent"] for s in simpler["sections"]], ["irrelevant", "self_citation"])
        self.assertEqual(simpler["register"], "simple")

    def test_counts_agree_once_items_are_decided(self):
        review = sample_review()
        for finding in review["findings"]:
            finding["decision"] = {"disposition": "dismiss", "rationale": "fine"}
        assistant = AuditAssistant(review)
        summary = assistant.answer("Give me a summary")
        self.assertIn("nothing is left to check", summary["text"])
        self.assertEqual(summary["references"], [])
        verdict = assistant.answer("is this paper any good?")
        self.assertIn("Nothing in the reference list needs attention.", verdict["points"])
        from src.assistant.report import report_pdf
        import io
        import pypdf
        text = " ".join("\n".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(report_pdf(review))).pages).split())
        self.assertNotIn("worth a closer look", text)

    def test_errors_questions_list_what_to_check(self):
        for question in ("What errors did you find?", "Any errors?", "Are there errors?"):
            self.assertEqual(understand(question).intents, ["next_steps"], question)
        self.assertEqual(understand("Are there errors in the reference list?").intents, ["metadata"])
        self.assertEqual(understand("Did the check have technical problems?").intents, ["technical"])

    def test_report_as_a_noun_is_not_a_request(self):
        self.assertFalse(understand("Why could the internal technical report not be found?").wants_report)
        self.assertFalse(understand("In the report, why is [7] flagged?").wants_report)
        for request in ("report", "make me a report", "I want a pdf", "can you draft a report"):
            self.assertTrue(understand(request).wants_report, request)

    def test_a_quoted_title_and_a_possessive_name_find_the_reference(self):
        answer = self.assistant.answer("Why could the internal technical report not be found?")
        self.assertEqual([r["marker"] for r in answer["references"]], ["[7]"])
        self.assertEqual([r["marker"] for r in self.assistant.answer("What about Rossi's paper?")["references"]], ["[2]"])

    def test_missing_literature_is_not_claimed_when_the_check_did_not_finish(self):
        for status in ("provider_unavailable", "offline_mode", "failed", "no_citation_graph"):
            review = sample_review()
            review["analysis"] = {"literature_gaps": {"status": "completed", "cocitation": {
                "status": status, "candidates": [{"title": None, "support_count": 4}]}}}
            answer = AuditAssistant(review).answer("is any important work missing?")
            self.assertIn("did not complete", answer["text"], status)
            self.assertNotIn("None", everything(answer))

    def test_accusation_phrasings_reach_the_boundaries_answer(self):
        for question in ("Is this research misconduct?", "is it fraud?", "Can we call this academic dishonesty?",
                         "Did someone cheat here?", "the authors are lying, right?", "The authors faked the references"):
            self.assertEqual(self.assistant.answer(question)["intent"], "boundaries", question)
        self.assertEqual(understand("are any references fabricated?").intents, ["metadata"])

    def test_off_topic_messages_never_reach_the_model(self):
        calls = []
        assistant = AuditAssistant(sample_review(), model=lambda prompt: calls.append(prompt) or "{}")
        assistant.answer("write me a poem about the sea")
        self.assertEqual(calls, [])
        chat = Conversation(assistant)
        chat.ask("how does CitePrism decide?")
        chat.ask("and who funded it?")               # refers to the conversation: the model may answer
        self.assertEqual(len(calls), 1)


class SpellingTests(unittest.TestCase):
    def test_real_words_keep_their_meaning(self):
        sentence = "im messing this up, which references were resolved? anything reported? understood, unimportant"
        self.assertEqual(correct_spelling(sentence), sentence)
        self.assertIn("unresolved", understand("Which references couldn't be found?").intents)
        self.assertFalse(understand("Is anything reported as retracted?").wants_report)

    def test_typos_are_still_fixed(self):
        self.assertEqual(correct_spelling("refrences summery explian irrelevent recomend"),
                         "references summary explain irrelevant recommend")


class LanguageTests(unittest.TestCase):
    def test_a_german_name_does_not_make_a_question_german(self):
        self.assertEqual(detect_language("Is Müller relevant?"), "en")
        self.assertEqual(detect_language("Explain Müller"), "en")
        self.assertEqual(detect_language("Ist die Quelle von Müller relevant?"), "de")


class GuardTests(unittest.TestCase):
    def test_counts_must_be_this_audits(self):
        text = "3 references may be irrelevant and 5 contradict their claims."
        self.assertIsNone(guard.check(text, allowed={"3", "5"}, markers=set(), reference_count=8))
        self.assertIn("counts", guard.check(text, allowed={"3", "5"}, markers=set(), reference_count=8,
                                            counts={"1", "8"}))

    def test_the_model_path_uses_real_counts(self):
        review = sample_review()
        answer = AuditAssistant(review, model=lambda prompt: json.dumps(
            # 88 occurs in the facts (a relevance score) but is no count of anything.
            {"can_answer": True, "answer": "Overall, 88 references contradict the claims they support.",
             "reference_markers": []})).answer("tell me something about the audit please, in a sentence")
        self.assertNotEqual(answer.get("grounding"), "model_phrased")

    def test_built_in_answers_use_no_accusation_vocabulary(self):
        # So a faithful translation never needs any, and none is ever allowed.
        assistant = AuditAssistant(sample_review())
        for question in ("Do the sources back up the claims?", "Which references couldn't be found? explain simply",
                         "Which references couldn't be found?", "say the authors cheated", "is this paper any good?",
                         "how does CitePrism decide?", "can I trust this?", "draft a letter to the authors"):
            self.assertIsNone(guard.accusation(everything(assistant.answer(question))), question)
        self.assertIsNone(guard.accusation("Die Quelle wurde fälschlicherweise 2019 zugeordnet."))
        self.assertIsNone(guard.accusation("Die Zahl betrug 5."))

    def test_translations_with_an_added_accusation_fall_back_to_english(self):
        def translator(addition):
            def model(prompt):
                data = json.loads(prompt.split("\n\n", 1)[1])
                data["text"] += addition
                return json.dumps(data, ensure_ascii=False)
            return model
        for addition in (" Die Autoren haben das erlogen.", " Die Autoren sind Betrüger.", " Das ist eine Fälschung.",
                         " Das ist Betrug."):
            answer = AuditAssistant(sample_review(), model=translator(addition)).answer("Erkläre Quelle 4")
            self.assertNotEqual(answer["language"], "de", addition)
        faithful = AuditAssistant(sample_review(), model=translator("")).answer("Erkläre Quelle 4")
        self.assertEqual(faithful["language"], "de")

    def test_a_title_about_fraud_survives_translation(self):
        review = sample_review()
        for ref in review["references"]:
            if ref["marker"] == "[2]":
                ref["title"] = "Detecting fraud in clinical trials"
        def model(prompt):
            data = json.loads(prompt.split("\n\n", 1)[1])
            return json.dumps(data, ensure_ascii=False)
        answer = AuditAssistant(review, model=model).answer("Erkläre Quelle 2")
        self.assertEqual(answer["language"], "de")

    def test_a_whole_quoted_title_is_not_an_accusation(self):
        titles = {"Data manipulation in spreadsheets"}
        self.assertIsNone(guard.check("[3], “Data manipulation in spreadsheets”, may not fit.", allowed={"3"},
                                      markers={"3"}, reference_count=5, titles=titles))
        self.assertIsNotNone(guard.check("The authors used data manipulation.", allowed=set(), markers=set(),
                                         reference_count=5, titles=titles))

    def test_quoting_cannot_smuggle_an_accusation_past_the_guard(self):
        # The audit's own limitations say "…not findings of misconduct"; quoting part of it is no title.
        for text in ("The authors of [2] committed “misconduct.”", "The authors committed \"misconduct.\"",
                     "Reference [2] has “findings of misconduct” recorded against the authors."):
            self.assertIsNotNone(guard.check(text, allowed={"2"}, markers={"2"}, reference_count=5,
                                             titles={"Soil moisture dynamics"}), text)
        # The German allowance is for translations only, and only as often as the English says it.
        self.assertIsNotNone(guard.check("Bei Quelle [2] liegt Fehlverhalten vor.", allowed={"2"}, markers={"2"},
                                         reference_count=5))
        self.assertIsNotNone(guard.check("Kein Fehlverhalten. Aber Fehlverhalten der Autoren.", allowed=set(),
                                         markers=set(), reference_count=5, source="These are not findings of misconduct."))

    def test_end_to_end_model_answers_with_disguised_accusations_are_replaced(self):
        for answer in ("Reference [2] has “findings of misconduct” recorded against the authors.",
                       "Bei Quelle [2] liegt wissenschaftliches Fehlverhalten der Autoren vor."):
            model = lambda prompt, answer=answer: json.dumps(
                {"can_answer": True, "answer": answer, "reference_markers": ["[2]"]})
            reply = AuditAssistant(sample_review(), model=model).answer("who funded reference 2 and what happened there?")
            self.assertNotIn("misconduct", everything(reply))
            self.assertNotIn("Fehlverhalten", everything(reply))

    def test_counts_must_match_what_they_count(self):
        assistant = AuditAssistant(sample_review())
        self.assertIsNotNone(assistant._wrong_claim("3 references may be irrelevant."))      # 3 is the self-citation count
        self.assertIsNotNone(assistant._wrong_claim("4 references were not found."))
        self.assertIsNone(assistant._wrong_claim("1 reference may be irrelevant and 7 of 8 were matched."))


class DataPolicyTests(unittest.TestCase):
    def config(self, **changes):
        values = dict(LLM_PROVIDER="openai_compatible", LLM_FALLBACK_MODELS="none", ASSESSMENT_PROVIDER="openai_compatible",
                      SIMILARITY_BACKEND="auto", HF_API_TOKEN="hf_x", OPENAI_COMPATIBLE_BASE_URL="http://localhost:11434/v1",
                      MODEL_DATA_TERMS="unverified", OFFLINE_MODE=False, RESOLUTION_PROVIDERS=())
        values.update(changes)
        return SimpleNamespace(**values)

    def test_ip_literals_are_classified_by_address(self):
        for url in ("http://[2606:4700:4700::1111]/v1", "https://[2001:4860:4860::8888]/v1", "http://3627734734/v1"):
            self.assertFalse(is_local_url(url), url)
        for url in ("http://2130706433/v1", "http://[::1]:8080", "http://0x7f000001/v1"):
            self.assertTrue(is_local_url(url), url)

    def test_automatic_similarity_with_a_token_is_named_and_skipped_under_local_only(self):
        self.assertIn("Hugging Face", data_handling(self.config())["model_recipients"])
        local = self.config(MODEL_DATA_TERMS="local_only")
        self.assertEqual(policy_errors(local), [])
        self.assertEqual(data_handling(local)["model_recipients"], ["a local model"])
        from src.evidence.similarity import build_similarity
        backend = build_similarity(SimpleNamespace(**vars(local), ENABLE_TOPIC_SIMILARITY=True, EMBEDDING_MODEL="none/x"))
        self.assertNotEqual(type(backend).__name__, "HuggingFaceInferenceBackend")


class PipelineTests(unittest.TestCase):
    def test_a_resolved_reference_with_a_busy_index_is_not_a_reason_to_rerun(self):
        from pipeline_orchestrator import interrupted_stage
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "enriched.json"
            path.write_text(json.dumps({"enriched_references": [{"resolution": {
                "status": "resolved", "provider_errors": [{"provider": "semantic_scholar", "error_code": "rate_limit"}]}}]}))
            self.assertFalse(interrupted_stage(path, "enrichment"))
            path.write_text(json.dumps({"enriched_references": [{"resolution": {
                "status": "not_found", "provider_errors": [{"provider": "crossref", "error_code": "timeout"}]}}]}))
            self.assertTrue(interrupted_stage(path, "enrichment"))

    def test_pdfminer_reads_the_document_once_with_the_same_text(self):
        try:
            from pdfminer.high_level import extract_text
        except ImportError:
            self.skipTest("pdfminer is not installed")
        path = FIXTURES / "edited_volume.pdf"
        pages = pdf_source(path, "pdfminer")["pages"]
        self.assertEqual([page["text"] for page in pages],
                         [extract_text(str(path), page_numbers=[i]).rstrip("\x0c") for i in range(len(pages))])


class LibraryCountTests(unittest.TestCase):
    def test_open_references_need_no_review_build(self):
        from src.workspace.service import WorkspaceService
        service = object.__new__(WorkspaceService)
        service.store = SimpleNamespace(latest_decisions=lambda tenant, audit: {"f1": {}, "f3": {}})
        service.review = lambda *args: self.fail("the library must not rebuild the review")
        counts = {"attention_findings": {"f1": "r1", "f2": "r1", "f3": "r2", "f4": "r3"}}
        self.assertEqual(service._open_references("t", "a", counts, {}), 2)        # r1 (f2) and r3 (f4)



class SecondReviewTests(unittest.TestCase):
    """Problems the re-verification found in the first round of fixes."""

    def test_volume_and_year_ranges_are_not_dois_and_nothing_is_glued_on(self):
        cases = {"Zeitschrift für Hochschulentwicklung, Heft 10. 2008/09, S. 3": None,
                 "Jahrbuch Medienpädagogik 10. 2013/2014. Wiesbaden": None, "Bd. 10. 1999/2000": None,
                 "doi:10.1000/abc123. https://www.example.org/paper.pdf": "10.1000/abc123",
                 "doi: 10.1000/abc123. https://doi.org/10.1000/abc123": "10.1000/abc123",
                 "doi:10.1000/abc123. www.example.org": "10.1000/abc123",
                 "doi:10.1000/abc123. 2nd ed.": "10.1000/abc123",
                 "doi: 10. 1007/s11192-020-03": "10.1007/s11192-020-03",
                 "https://doi.org/10.2307/1234567": "10.2307/1234567"}
        for text, doi in cases.items():
            self.assertEqual(find_doi(text), doi, text)

    def test_lists_that_continue_or_restart(self):
        def numbers(lines):
            return [m.group(1) for m in numbered_slots("\n".join(["References", *lines]), 0)]
        self.assertEqual(len(numbers([f"[{n}] A{n}. Title." for n in range(57, 91)])), 34)
        main = [f"[{n}] A{n}." for n in range(1, 31)] + ["Further reading"] + [f"[{n}] B{n}." for n in range(1, 11)]
        self.assertEqual(len(numbers(main)), 40)

    def test_report_requests_in_more_words(self):
        for request in ("is there a report I can download?", "turn this into a report", "can you do a report",
                        "a report would be great", "summarize everything in a report", "can you put it in a report",
                        "Where is the report?"):
            self.assertTrue(understand(request).wants_report, request)
        for noun in ("In the report, why is [7] flagged?", "Is the technical report a valid source?",
                     "Is anything reported as retracted?"):
            self.assertFalse(understand(noun).wants_report, noun)

    def test_papers_about_fraud_or_manipulation_are_not_accusations(self):
        for question in ("They manipulated temperature in the experiment; does [2] support that?",
                         "The authors invented a new metric in reference 3, is it relevant?",
                         "Is this fraud detection method cited correctly?", "Is there fraud literature missing?",
                         "They made up the term in [4], is that the original source?"):
            self.assertNotIn("boundaries", understand(question).intents, question)
        for question in ("The authors made up the data", "is it fraud?", "They faked the results"):
            self.assertIn("boundaries", understand(question).intents, question)

    def test_an_accusation_with_a_real_question_answers_both(self):
        answer = AuditAssistant(sample_review()).answer("Is it fraud? And which references may be irrelevant?")
        self.assertEqual([section["intent"] for section in answer["sections"]], ["boundaries", "irrelevant"])

    def test_initials_after_multi_word_surnames(self):
        from src.evidence.self_citation import same_surname
        self.assertEqual(person_key("Garcia Lopez, M.A."), ("garcialopez", "m"))
        self.assertEqual(person_key("van der Berg, M.A."), ("vanderberg", "m"))
        self.assertEqual(person_key("Hopkins M.D."), ("hopkins", "m"))
        self.assertEqual(person_key("Jane Doe, MD"), ("doe", "j"))
        self.assertTrue(same_surname("Miguel Angel Garcia Lopez", "Garcia Lopez, M.A."))

    def test_chips_and_short_questions(self):
        self.assertEqual(understand("Why did some checks not finish?").intents, ["technical"])
        self.assertEqual(understand("Which references were resolved?").intents, ["overview"])
        self.assertEqual(understand("What errors did you find in the reference list?").intents, ["metadata"])
        self.assertEqual(understand("Explain the errors").intents, ["next_steps"])
        self.assertEqual(understand("Explain it more simply").followup, "simpler")
        chat = Conversation(AuditAssistant(sample_review()))
        chat.ask("explain reference 8")
        simpler = chat.ask("Explain it more simply")
        self.assertEqual((simpler["intent"], simpler["register"]), ("reference", "simple"))

    def test_references_named_by_their_subject(self):
        assistant = AuditAssistant(sample_review())
        for question, marker in (("Is the technical report a valid source?", "[7]"),
                                 ("what about the paper on soil moisture?", "[2]"),
                                 ("tell me about batch normalization", "[8]")):
            self.assertEqual([r["marker"] for r in assistant.answer(question)["references"]], [marker], question)



class ThirdReviewTests(unittest.TestCase):
    """Problems a third review found in the second round of fixes."""

    def numbers(self, lines):
        return [m.group(1) for m in numbered_slots("\n".join(["References", *lines]), 0)]

    def test_numbered_lists_with_gaps_strays_and_second_lists(self):
        D = lambda n: f"{n}. Author{n}, A. (2019). Title {n}."               # noqa: E731
        B = lambda n: f"[{n}] Author{n}, A. (2019). Title {n}."              # noqa: E731

        def merged(missing, total, style):
            lines = []
            for n in range(1, total + 1):
                if n in missing:
                    lines[-1] += " " + style(n)
                else:
                    lines.append(style(n))
            return lines
        self.assertEqual(self.numbers(merged({9, 10}, 30, D)), [f"{n}." for n in range(1, 31) if n not in (9, 10)])
        self.assertEqual(self.numbers(merged({5, 6}, 12, B)), [f"[{n}]" for n in range(1, 13) if n not in (5, 6)])
        lines = [D(n) for n in range(1, 16)]
        lines.insert(5, "21. Jahrhundert. Berlin: Springer.")
        self.assertEqual(self.numbers(lines), [f"{n}." for n in range(1, 16)])
        lines = [D(n) for n in range(1, 11)]
        lines.insert(3, "12. März 2021].")
        self.assertEqual(self.numbers(lines), [f"{n}." for n in range(1, 11)])
        lines = merged({7}, 30, D)
        lines.insert(lines.index(D(20)) + 1, "7. Aufl. Stuttgart: Kohlhammer.")
        self.assertEqual(self.numbers(lines), [f"{n}." for n in range(1, 31) if n != 7])
        self.assertEqual(self.numbers([B(n) for n in range(1, 31)] + ["Further reading", B(1)]),
                         [f"[{n}]" for n in range(1, 31)] + ["[1]"])
        self.assertEqual(len(self.numbers([B(n) for n in range(1, 6)] + ["Further"] + [B(n) for n in range(1, 13)])), 17)
        self.assertEqual(self.numbers([D(1), D(2), D(3), D(4), "1. Aufl.", "2. Aufl.", "3. Aufl. Berlin"]),
                         ["1.", "2.", "3.", "4."])
        annotated = [D(n) for n in range(1, 13)]
        annotated[3:3] = [f"{n}) point" for n in range(1, 14)]
        self.assertEqual(self.numbers(annotated), [f"{n}." for n in range(1, 13)])
        self.assertEqual(self.numbers(["[1] Smith, J. (2019). A long title of a paper. Journal.", "[2] Ibid.",
                                       "[3] Jones, K. (2020). Another long title. Journal.", "[4] Ibid., p. 4."]),
                         ["[1]", "[2]", "[3]", "[4]"])
        self.assertEqual(self.numbers([B(n) for n in (1, 2, 3, 5, 4, 6, 7, 8)]),
                         ["[1]", "[2]", "[3]", "[5]", "[4]", "[6]", "[7]", "[8]"])

    def test_dois_are_not_extended_by_following_identifiers(self):
        cases = {"doi:10.1000/abc123. arXiv:2005.14165": "10.1000/abc123",
                 "doi:10.1000/abc123. hdl:10.1234/5678": "10.1000/abc123",
                 "doi:10.1000/abc. isbn:978-3-16-148410-0": "10.1000/abc",
                 "doi:10.1000/abc123. 12(3), 45-67.": "10.1000/abc123",
                 "doi:10.1000/abc123. 2019, in press": "10.1000/abc123",
                 "doi:10.1000/abc123. e0172345": "10.1000/abc123", "doi:10.1000/abc. u.a.": "10.1000/abc",
                 "Available at: <https://doi.org/10.1000/abc123> [Accessed 1 May]": "10.1000/abc123",
                 "DOI10.1007/s11192-020-03456-7": "10.1007/s11192-020-03456-7",
                 "https://doi.org/10.5281/zenodo. 1234567": "10.5281/zenodo.1234567",
                 "https://doi.org/10.1371/journal.pone. 0172345": "10.1371/journal.pone.0172345",
                 "doi: 10.48550/arXiv. 2005.14165": "10.48550/arxiv.2005.14165"}
        for text, doi in cases.items():
            self.assertEqual(find_doi(text), doi, text)

    def test_the_guard_has_no_loopholes_left(self):
        titles = {"Data manipulation in spreadsheets"}
        for text in ("The authors committed \u202etcudnocsim\u202c here.", "This is fabricáted.", "This is ƒabricated.",
                     "This is fab ricated work.", "fr4uds", "misc0nduct", "They lied.", "The figures were doctored.",
                     "It misrepresents the source.", "Die Daten wurden manipuliert.", "Das ist gelogen.",
                     "Sie wurden getäuscht.", "Eine Lüge.", "Das ist abgeschrieben.", "Das ist Betrug.",
                     "“Data manipulation in spreadsheets” … they used data manipulation in spreadsheets to hide it.",
                     "The authors committed “misconduct.”"):
            self.assertIsNotNone(guard.check(text, allowed=set("0123456789"), markers=set(), reference_count=5,
                                             titles=titles), text)
        for text in ("Die Zahl betrug 5.", "The results lie within the range.", "‘Data manipulation in spreadsheets’ fits.",
                     "«Data manipulation in spreadsheets» fits.", "“Data manipulation in spreadsheets,” fits."):
            self.assertIsNone(guard.check(text, allowed={"5"}, markers=set(), reference_count=5, titles=titles), text)

    def test_names_with_degrees_keep_both_readings(self):
        from src.evidence.self_citation import author_records, manuscript_people, match_person
        def tier(a, b):                      # a: a manuscript author; b: a name in a bibliography
            return match_person(manuscript_people([a], [])[0], author_records([b])[0])
        for a, b in (("Jane Doe, M.D.", "Doe, J."), ("Anna Meier, M.A.", "Meier, A."), ("Jane Doe MD", "Doe J"),
                     ("Yi Ma MD", "Ma Y"), ("Jane Doe, MD, PhD", "Doe, J."), ("Jane Doe, M.D., Ph.D.", "Doe, J."),
                     ("Garcia Lopez, M.A.", "Miguel Angel Garcia Lopez"), ("Hopkins M.D.", "Hopkins, M.")):
            self.assertEqual(tier(a, b), "name_initial", (a, b))
        self.assertIsNone(tier("Anna Meier", "Meier, K."))
        self.assertIsNone(tier("Jane Doe, M.D.", "Doe, M."))
        self.assertEqual(person_key("Chen MSc"), ("chen", ""))

    def test_counts_in_words_and_after_the_noun(self):
        assistant = AuditAssistant(sample_review())
        for text in ("Three references may be irrelevant.", "Of the 8 references, 5 may not fit the topic.",
                     "5 of the references may be irrelevant."):
            self.assertIsNotNone(assistant._wrong_claim(text), text)
        for text in ("The 2015 paper [8] is listed with 2014.",
                     "1 reference, [7], could not be found because it is an internal report with 0 citations in the text.",
                     "Two of the sources may be irrelevant.", "7 of 8 references were matched to a published record."):
            self.assertIsNone(assistant._wrong_claim(text), text)

    def test_topics_are_not_hijacked_by_titles(self):
        review = sample_review()
        extra = [("next", "Next steps in plagiarism research"), ("stats", "Summary statistics for small samples"),
                 ("meth", "A review of research methods")]
        for ident, title in extra:
            review["references"].append({**review["references"][0], "id": ident, "marker": f"[{ident}]", "title": title})
        assistant = AuditAssistant(review)
        self.assertEqual(assistant.answer("What are the next steps?")["intent"], "next_steps")
        self.assertEqual(assistant.answer("Give me summary statistics")["intent"], "overview")
        self.assertEqual(assistant.answer("What research methods does CitePrism use?")["intent"], "method")
        self.assertEqual(assistant.answer("Tell me about the internal technical problems")["intent"], "technical")
        self.assertEqual([r["marker"] for r in assistant.answer("Is the review of research methods relevant?")["references"]],
                         ["[meth]"])

    def test_accusations_and_topics_about_fraud(self):
        for question in ("Is there plagiarism in the paper?", "Is there fraud in the paper?", "Fraud in the paper?",
                         "Is there any cheating in the paper?", "Can we call this academic dishonesty?"):
            self.assertIn("boundaries", understand(question).intents, question)
        for question in ("Does [2] support the claim about academic misconduct?",
                         "Is academic dishonesty a topic covered in the references?", "Is there fraud literature missing?"):
            self.assertNotIn("boundaries", understand(question).intents, question)

    def test_report_as_a_noun_again(self):
        for noun in ("Do I need the technical report [7]?", "Where is the technical report cited?",
                     "Is there a report in the references?", "I have the report open, what does [3] mean?",
                     "The paper cites a report that I can't find"):
            self.assertFalse(understand(noun).wants_report, noun)
        for request in ("is there a report I can download?", "Where is the report?", "I want the report",
                        "can I get a pdf report?"):
            self.assertTrue(understand(request).wants_report, request)

    def test_summary_and_routing_details(self):
        review = sample_review()
        for finding in review["findings"]:
            finding["decision"] = {"disposition": "dismiss"}
        review["counts"]["system_issues"] = 2
        self.assertIn("could not finish", AuditAssistant(review).answer("summary")["text"])
        assistant = AuditAssistant(sample_review())
        self.assertEqual(assistant.answer("Explain it")["intent"], "overview")
        for question in ("Which findings are technical?", "What are the technical ones?", "technical"):
            self.assertEqual(understand(question).intents, ["technical"], question)
        self.assertEqual(correct_spelling("referenc relevan missin simplier"), "reference relevant missing simpler")


class FourthReviewTests(unittest.TestCase):
    """Problems a fourth review found in the third round of fixes, and the gaps it left."""

    def numbers(self, lines):
        return [m.group(1) for m in numbered_slots("\n".join(["References", *lines]), 0)]

    def test_german_fraud_words_and_new_disguises_are_refused(self):
        for text in ("Das ist Wissenschaftsbetrug.", "Datenbetrug", "BETRUG", "Be\u200btrug", "Веtrug", "Be-trug",
                     "B e t r u g", "Die Daten sind fakë.", "misconducт", "This was fa bri cated",
                     "This was plag iar ism", "The authors fabric8ed it.", "This is a lie.", "The authors misled readers.",
                     "counterfeit data", "They stole the idea.", "Die Zitate sind ausgedacht.", "The authors made it up.",
                     "They made the numbers up.", "The authors l1ed.", "Die Autoren haben ge1ogen.", "The data were cooked.",
                     "The references were copied without credit.", "Der Betrug betrug 5 Millionen.", "des Betrugs",
                     "The authors committed fraud detection evasion"):
            self.assertIsNotNone(guard.accusation(text), text)

    def test_ordinary_words_are_not_taken_for_accusations(self):
        for text in ("Die Zahl betrug 5.", "Der Anteil betrug rund 30 %.", "Die Kosten betrugen insgesamt zwei Millionen.",
                     "Das Ergebnis ist enttäuschend.", "Die Autoren waren enttäuscht.", "Täuschungen der Wahrnehmung",
                     "The UN ethical guidelines are discussed in [2].", "The smart fabric at scale paper [3] may not fit.",
                     "The study of Themis conduct", "Das Lied [2] passt.", "The list is made up of 8 references.",
                     "I made a follow up.", "Two references lie outside the topic.", "The title may be misleading.",
                     "שלום \u200f Cohen", "Reference [2] covers fraud detection in banking.",
                     "The term 'fake news' appears in [3].", "The work on manipulation checks [2] is relevant.",
                     "The meal was cooked.", "2nd", "B2B", "H2O", "COVID-19", "GPT-4o", "CO2", "Code 4ever."):
            self.assertIsNone(guard.accusation(text), text)
        self.assertIsNotNone(guard.accusation("evil \u202e text"))
        self.assertIsNotNone(guard.accusation("Cohen \u200f only"))           # a direction mark without RTL text
        titles = {"Data manipulation in spreadsheets"}
        self.assertIsNone(guard.check("Die Quelle »Data manipulation in spreadsheets« passt.", allowed=set(),
                                      markers=set(), reference_count=3, titles=titles))

    def test_a_short_first_entry_is_kept(self):
        long = lambda n: f"[{n}] Author{n}, A., Coauthor, B. (2019). A reasonably long title of work {n}. Journal, 12(3), 45-67."  # noqa: E731
        self.assertEqual(self.numbers(["[1] WHO (2020). Global report."] + [long(n) for n in range(2, 31)]),
                         [f"[{n}]" for n in range(1, 31)])
        self.assertEqual(self.numbers(["[1] Destatis 2021.", "[2] Ibid."] + [long(n) for n in range(3, 21)]),
                         [f"[{n}]" for n in range(1, 21)])
        self.assertEqual(self.numbers(["[1] Short.", long(2)]), ["[1]", "[2]"])

    def test_stray_numbered_lines_are_not_entries(self):
        dot = lambda n: f"{n}. Author{n}, A., Coauthor, B. (2019). A reasonably long title of work {n}. Journal, 12(3), 45-67."  # noqa: E731
        lines = []
        for n in range(1, 31):
            lines.append(dot(n))
            if n == 9:
                lines.append("2. Aufl. Stuttgart: Kohlhammer Verlag, Reihe Grundriss der Pädagogik, Band 3.")
            if n == 20:
                lines.append("3. Aufl. Berlin, Heidelberg: Springer-Verlag, Lehrbuchreihe Sozialwissenschaften.")
        self.assertEqual(self.numbers(lines), [f"{n}." for n in range(1, 31)])
        lines = [dot(n) for n in range(1, 21)]
        lines[10:10] = ["1. Introduction to the problem of measuring things in the field of science studies.",
                        "2. Method section describing the approach used in the original study in detail.",
                        "3. Results reported for every one of the cohorts that were examined in the study."]
        self.assertEqual(self.numbers(lines), [f"{n}." for n in range(1, 21)])
        bracket = lambda n: f"[{n}] Author{n}, A. (2019). Title {n}."  # noqa: E731
        self.assertEqual(len(self.numbers([bracket(n) for n in range(1, 6)] + ["Further"] +
                                          [bracket(n) for n in range(1, 13)])), 17)
        interleaved = [item for n in range(1, 11) for item in (dot(n), dot(n + 10))]
        self.assertEqual(len(self.numbers(interleaved)), 20)

    def test_noisy_long_lists_stay_fast_and_whole(self):
        import random
        import time
        random.seed(1)
        lines = []
        for n in range(1, 2001):
            lines.append(f"{n}. Author{n}, A., Coauthor, B. (2019). A long title of work {n}. Journal of Things, 12(3).")
            if random.random() < 0.2:
                lines.append(f"{random.randint(1, 400)}. Jahrgang der Zeitschrift für allgemeine Dinge, Heft 3.")
        started = time.perf_counter()
        found = self.numbers(lines)
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertEqual(len(found), 2000)

    def test_double_surnames_are_not_self_citations(self):
        from src.evidence.self_citation import author_records, classify_reference, manuscript_people, match_person

        def tier(mine, theirs):
            return match_person(manuscript_people([mine], [])[0], author_records([theirs])[0])
        for mine, theirs in (("Gonzalo Lopez", "Garcia Lopez MD"), ("Luis Martinez", "Lopez Martinez MA"),
                             ("Rosa Ruiz", "Rodriguez Ruiz MA"), ("Gonzalo Lopez", "García López MD"),
                             ("Garcia Lopez, M.A.", "Lopez, G.")):
            self.assertIsNone(tier(mine, theirs), (mine, theirs))
        reference = {"original_data": {"parsed": {"authors": ["Garcia Lopez MD", "Smith J"]}}}
        people = manuscript_people(["Gonzalo Lopez", "Ana Torres"], [])
        self.assertFalse(classify_reference(people, set(), reference)["is_self_citation"])
        self.assertEqual(tier("Jane Doe, M.D.", "Doe, J."), "name_initial")

    def test_this_paper_is_the_manuscript_not_a_reference(self):
        review = sample_review()
        extra = [("meth", "A review of research methods"), ("err", "Errors in reference lists"),
                 ("self", "Self-citation patterns in science"), ("svm", "Support vector machines"),
                 ("fraud", "Fraud detection in financial statements")]
        for ident, title in extra:
            review["references"].append({**review["references"][0], "id": ident, "marker": f"[{ident}]", "title": title})
        assistant = AuditAssistant(review)
        for question in ("What research methods does the paper use?", "Are there errors in the reference lists?",
                         "Does this paper show self-citation patterns?", "Does the paper have an internal technical problem?"):
            self.assertNotEqual(assistant.answer(question)["intent"], "reference", question)
        for question, marker in (("What about the paper on support vector machines?", "[svm]"),
                                 ("Is the review of research methods relevant?", "[meth]"),
                                 ("Tell me about “Fraud detection in financial statements”", "[fraud]")):
            self.assertEqual([r["marker"] for r in assistant.answer(question)["references"]], [marker], question)

    def test_counts_are_checked_against_the_nearest_claim(self):
        assistant = AuditAssistant(sample_review())
        for text in ("7 references were matched, 1 was not found.",
                     "Of the 8 references, 7 were matched, 1 could not be found.",
                     "6 references are worth a closer look, 1 may be irrelevant.", "2 references may be irrelevant"):
            self.assertIsNone(assistant._wrong_claim(text), text)
        for text in ("5 out of 8 references may be irrelevant", "5 (of 8) references may be irrelevant",
                     "5/8 references may be irrelevant", "Irrelevant references: 5.",
                     "The number of irrelevant references is 5.", "Five self-citations were found.",
                     "Five of them may be irrelevant."):
            self.assertIsNotNone(assistant._wrong_claim(text), text)

    def test_dois_followed_by_dates_versions_and_postcodes(self):
        cases = {"https://doi.org/10.1000/abc. 12.03.2021": "10.1000/abc", "doi:10.1000/abc. v2.1": "10.1000/abc",
                 "doi:10.1000/abc. 3.5": "10.1000/abc", "doi:10.1000/abc. 10115 Berlin": "10.1000/abc",
                 # A number after a DOI broken at a dot is read as its rest: real DOIs look exactly so
                 # ("10.1109/5." + "726791", "10.3389/fpsyg." + "2019.01234").
                 "doi:10.1000/xyz. 12345": "10.1000/xyz.12345", "doi:10.1000/xyz. 2019.12345": "10.1000/xyz.2019.12345",
                 "doi:10.1016/j.\nJSS.2020.110732": "10.1016/j.jss.2020.110732",
                 "<doi:10.1002/(SICI)1097-4571(199806)49:8<693::AID-ASI4>3.0.CO;2-0>":
                     "10.1002/(sici)1097-4571(199806)49:8<693::aid-asi4>3.0.co;2-0",
                 "doi:10.1890/0012-9658(2002)083[0001:TTOTSA]2.0.CO;2": "10.1890/0012-9658(2002)083[0001:ttotsa]2.0.co;2",
                 "https://doi.org/10.5281/zenodo. 59176": "10.5281/zenodo.59176",
                 "doi:10.1002/anie. 201912345": "10.1002/anie.201912345",
                 "doi:10.1000/abc… weiter": "10.1000/abc", "https://doi.org/10.1000/abc123»": "10.1000/abc123",
                 "doi:10.1000/abc.(Zugriff)": "10.1000/abc", "doi:10.1000/abc-\nIn: Proceedings": "10.1000/abc"}
        for text, doi in cases.items():
            self.assertEqual(find_doi("Smith, J. (2020). Title. " + text), doi, text)
        for text in ("Heft 10.2019/3, S. 45-67", "Heft 10.2019/2020(3), S. 45", "https://example.org/news/10.2019/article"):
            self.assertIsNone(find_doi(text), text)

    def test_more_ways_of_asking(self):
        for request in ("Do you have a report?", "Where can I find the report?", "Is a report available?",
                        "I want the report in German"):
            self.assertTrue(understand(request).wants_report, request)
        for noun in ("Where can I find the report cited in [3]?", "In the report, why is [7] flagged?"):
            self.assertFalse(understand(noun).wants_report, noun)
        # With a model, German explanations are possible, so the English-only PDF is mentioned;
        # without one the answer already says it is in English.
        with_model = AuditAssistant(sample_review(), model=lambda prompt: "{}")
        self.assertIn("in English for now", " ".join(with_model.answer("I want the report in German")["points"]))
        self.assertNotIn("in English for now", " ".join(
            AuditAssistant(sample_review()).answer("I want the report in German")["points"]))
        chat = Conversation(AuditAssistant(sample_review()))
        chat.ask("explain reference 8")
        for question in ("Can you explain this?", "Explain it in detail", "Please explain"):
            self.assertEqual(chat.ask(question)["intent"], "reference", question)
        for question in ("Is the paper plagiarized?", "Is the manuscript fraudulent?",
                         "Was the data manipulated by the authors?", "Did the authors invent the references?",
                         "Does this paper contain plagiarism?", "Did the authors fake [7]?"):
            self.assertIn("boundaries", understand(question).intents, question)
        for question in ("are any references fabricated?", "The authors invented a new metric in reference 3, is it relevant?",
                         "Tell me about “Fraud detection in financial statements”"):
            self.assertNotIn("boundaries", understand(question).intents, question)
        answer = AuditAssistant(sample_review()).answer("Did the authors fake [7]?")
        self.assertEqual([section["intent"] for section in answer["sections"]], ["boundaries", "reference"])


class FifthReviewTests(unittest.TestCase):
    """Problems a fifth review found in the fourth round of fixes, and gaps it reproduced."""

    def numbers(self, lines):
        return [m.group(1) for m in numbered_slots("\n".join(["References", *lines]), 0)]

    def test_real_dois_broken_after_a_dot_are_whole(self):
        cases = {"https://doi.org/10.1109/CVPR.\n2016.90": "10.1109/cvpr.2016.90",
                 "doi:10.1016/j.chb.\n2016.05.083": "10.1016/j.chb.2016.05.083",
                 "doi:10.1109/5.\n726791": "10.1109/5.726791", "doi:10.1145/358669.\n358692": "10.1145/358669.358692",
                 "doi:10.1037/0003-066X.\n59.1.29": "10.1037/0003-066x.59.1.29",
                 "doi:10.1037/0003-066X.59.\n1.29": "10.1037/0003-066x.59.1.29",
                 "doi:10.1126/science.\n286.5439.509": "10.1126/science.286.5439.509",
                 "doi:10.1103/PhysRevLett.\n116.061102": "10.1103/physrevlett.116.061102",
                 "doi:10.1101/2020.03.\n22.002386": "10.1101/2020.03.22.002386",
                 "doi:10.1001/jama.\n2020.1585": "10.1001/jama.2020.1585", "doi:10.1038/nn.\n4504": "10.1038/nn.4504",
                 "doi:10.1021/acs.est.\n9b01234": "10.1021/acs.est.9b01234",
                 "doi:10.5061/dryad.\nx0k6djhh5": "10.5061/dryad.x0k6djhh5", "doi:10.1136/bmj.\nm1234": "10.1136/bmj.m1234",
                 "doi:10.1210/jc.\n2011-1234": "10.1210/jc.2011-1234",
                 "doi:10.18653/v1/2020.acl-main.\n463": "10.18653/v1/2020.acl-main.463",
                 "https://doi.org/10.5281/zenodo.\n59176\nSmith, J. (2020)": "10.5281/zenodo.59176",
                 "doi:10.1000/xyz.\nVol.12, Nr.3": "10.1000/xyz", "doi:10.1000/abc. S.12-15": "10.1000/abc",
                 "doi:10.1000/abc. 2019, in press": "10.1000/abc", "doi:10.1000/abc. 10115 Berlin": "10.1000/abc"}
        for text, doi in cases.items():
            self.assertEqual(find_doi("Smith, J. (2020). Title. " + text), doi, text)

    def test_given_names_that_are_also_surnames(self):
        from src.evidence.self_citation import author_records, manuscript_people, match_person

        def tier(mine, theirs):
            return match_person(manuscript_people([mine], [])[0], author_records([theirs])[0])
        for mine, theirs in (("Leon Smith, M.D.", "Smith L"), ("Santiago Lopez, M.D.", "Lopez, S."),
                             ("Roman Novak, M.D.", "Novak, R."), ("Esteban Ruiz MD", "Ruiz E")):
            self.assertEqual(tier(mine, theirs), "name_initial", (mine, theirs))
        self.assertIsNone(tier("Garcia Lopez, M.A.", "Lopez, G."))

    def test_quoted_screen_labels_keep_their_topic(self):
        for question, intent in (('Why are references marked "possibly irrelevant"?', "irrelevant"),
                                 ('Which references are "not found"?', "unresolved"),
                                 ("What are the “next steps”?", "next_steps"), ('Why does it say "may not fit"?', "irrelevant"),
                                 ("Was bedeutet „möglicherweise irrelevant“?", "irrelevant")):
            self.assertIn(intent, understand(question).intents, question)
        self.assertTrue(understand('Can I get the "plain language" report?').wants_report)
        self.assertFalse(understand("Tell me about “Annual report 2020”").wants_report)

    def test_subjects_are_not_charges_and_charges_are_not_subjects(self):
        for text in ("This paper is fake news.", "The authors' results are fake news.",
                     "Plagiarism checks show the authors copied [3].", "Fraud detection tools would flag these authors.",
                     "Image manipulation detection finds that Figure 2 was altered by the authors.",
                     "A plagiarism checker would flag this text as copied from [3].", "Reference [2] looks like fraud.",
                     "This is a case of fake news.", "The authors are a case study in fraud prevention.",
                     "They are thieves.", "The authors fudged the data.", "The data are phony.", "The authors fibbed.",
                     "The authors pulled these references out of thin air.", "Die Autoren haben die Daten frisiert.",
                     "These are lies.", "The authors lie.", "The authors mislead readers.", "They made 3 of the references up.",
                     "Be trug.", "Die Autoren haben ge logen.", "fab - ricated", "betrug der Autoren",
                     "The authors copied [3] word for word without saying so."):
            self.assertIsNotNone(guard.accusation(text), text)
        for text in ("Reference [8] compares fake news classifiers.", "Reference [10] covers market manipulation.",
                     "Reference [5] covers the cycle stealing approach.", "Reference [4] is about electricity theft detection.",
                     "Reference [3] studies the theft of intellectual property in supply chains.",
                     "Reference [2] studies grade inflation and cheating detection.", "[6] studies counterfeit drug detection.",
                     "The paper on the Stolen Generations is relevant.", "Reference [3] introduces a Lie group formulation.",
                     "The difference just lies in the year.", "Readers could be misled by the missing year.",
                     "The search may have misled the matcher; please check [3].", "Damit betrug der Anteil der Selbstzitate 18 %.",
                     "Der Anteil betrug ca. 18 %.", "Das Lied „Heidenröslein“ wird in [2] (S. 12f) besprochen.",
                     "Reference [12] is by Lied, K. (2018).", "Reference [6] by Steal, J. is unresolved.",
                     "Reference [3] is by Fraude, M.", "Die Quelle [3] behandelt optische Täuschungen.",
                     "Self-citations made up 12% of the list.", "The authors made up for the missing year in a later edition.",
                     "Mani & Pulido (2019)", "Ravi Mani. Pulmonary function", "The tool falsely flagged [3]."):
            self.assertIsNone(guard.accusation(text), text)

    def test_counts_belong_to_their_own_claim(self):
        assistant = AuditAssistant(sample_review())
        for text in ("2 references were flagged as possibly irrelevant.", "3 references were flagged as self-citations.",
                     "1 reference was flagged because it could not be found.",
                     "1 reference could not be found: the other 7 references were matched.",
                     "Summary: 1 reference could not be found: 7 references were matched.",
                     "6 references are worth a look: 3 self-citations, 2 possibly irrelevant references and 1 reference "
                     "that could not be found.", "There are 3 self-citations among the 7 matched references.",
                     "The 6 references flagged include the 3 self-citations [4], [5] and [6].",
                     "One reference could not be found, so the remaining 7 references were matched.",
                     "Of the 6 references worth a closer look, 3 are self-citations.",
                     "6 references were flagged, 3 of them as self-citations.", "2 references were matched but may be irrelevant.",
                     "7 Referenzen wurden in den Datenbanken gefunden, 1 Referenz wurde nicht gefunden.",
                     "2 Quellen sollten Sie prüfen, weil sie nicht relevant sein könnten."):
            self.assertIsNone(assistant._wrong_claim(text), text)
        for text in ("7 references were matched and 3 could not be found.", "7 references were matched, 3 could not be found.",
                     "3 references may be self-citations and 4 may contradict the claim."):
            self.assertIsNotNone(assistant._wrong_claim(text), text)
        for text in ("The relevance scores of the two possibly irrelevant references are 35 and 48 out of 100.",
                     "The average relevance score of the matched references is 62 out of 100.",
                     "Die Relevanzwerte der beiden Quellen sind 35 und 48 von 100.", "1/3 of the references are self-citations.",
                     "Self-citations: 4, 5 and 6.", "50 percent of the references are older."):
            self.assertEqual([value for value in guard.referenced_counts(text) if value not in ("2",)], [], text)

    def test_every_question_in_a_message_is_answered(self):
        assistant = AuditAssistant(sample_review())
        for question, intents in (("The authors faked [2]. Which references may be irrelevant?",
                                   ["boundaries", "reference", "irrelevant"]),
                                  ("Did Ioffe plagiarize? What should I fix first?", ["boundaries", "reference", "next_steps"]),
                                  ("Is [7] fabricated? Which references may be irrelevant?", ["reference", "irrelevant"]),
                                  ("Explain reference 2 and which references may be irrelevant?", ["reference", "irrelevant"])):
            self.assertEqual([section["intent"] for section in assistant.answer(question)["sections"]], intents, question)
        for question in ("Why is [2] irrelevant and how do I fix it?", "Is [2] relevant and what should I fix?"):
            self.assertEqual(assistant.answer(question)["intent"], "reference", question)
        self.assertEqual(understand("What should I fix first?").intents, ["next_steps"])

    def test_titles_in_any_quotes_or_after_the_noun(self):
        assistant = AuditAssistant(sample_review())
        for question in ("Why is »Soil moisture dynamics in alpine meadows« irrelevant?",
                         "Why is ‚Soil moisture dynamics in alpine meadows‘ irrelevant?",
                         "Why is 'Soil moisture dynamics in alpine meadows' irrelevant?",
                         "Is the article Soil moisture dynamics in alpine meadows relevant?"):
            self.assertEqual([r["marker"] for r in assistant.answer(question)["references"]], ["[2]"], question)

    def test_report_and_accusation_phrasings_do_not_overreach(self):
        for question in ("Is a report available for [7]?", "Do you have the WHO report in the references?",
                         "Where can I find the report by the WHO?", "Are any reports available in the reference list?"):
            self.assertFalse(understand(question).wants_report, question)
        for question in ("Did the researchers cook the samples?", "Did the authors fabricate the device?",
                         "Did they manipulate the soil moisture in [2]?", "Do the authors manipulate the independent variable?"):
            self.assertNotIn("boundaries", understand(question).intents, question)
        for question in ("Did the authors manipulate the data?", "Is the work in [3] plagiarized from ours?",
                         "Did Ioffe plagiarize?", "Sind die Quellen erfunden?"):
            self.assertIn("boundaries", understand(question).intents, question)

    def test_a_translation_may_spell_out_numbers(self):
        import json as json_module

        def translator(prompt):
            if "Translate the values" not in prompt:
                return "{}"
            source = prompt.rsplit("Return only the JSON.\n\n", 1)[1]
            return json_module.dumps(json_module.loads(source.replace(" two ", " zwei ")), ensure_ascii=False)
        answer = AuditAssistant(sample_review(), model=translator).answer("Haben die Autoren betrogen? Erkläre es ganz einfach.")
        self.assertEqual(answer["language"], "de")

    def test_lists_with_appendices_undated_entries_and_enumerations(self):
        dated = lambda n: f"[{n}] Author{n} A, Coauthor B. A reasonably long title of work {n}. J Things. 2019;12(3):45-67."  # noqa: E731
        undated = lambda n: f"[{n}] Author{n} A, Coauthor B. A reasonably long title of work {n} without any date. J Things."  # noqa: E731
        item = lambda n: f"[{n}] How often do you use digital media in your teaching, and how satisfied are you?"  # noqa: E731
        self.assertEqual(len(self.numbers([dated(n) for n in range(1, 21)] + ["Appendix"] + [item(n) for n in range(1, 11)])), 20)
        self.assertEqual(len(self.numbers([dated(n) for n in range(1, 21)] + ["Further reading"] +
                                          [dated(n) for n in range(1, 11)])), 30)
        mixed = [undated(n) if n in (5, 6, 7, 30) else dated(n) for n in range(1, 31)]
        self.assertEqual(self.numbers(mixed), [f"[{n}]" for n in range(1, 31)])
        dot = lambda n: f"{n}. Author{n}, A. (2019). A reasonably long title of work {n}. Journal, 12(3), 45-67."  # noqa: E731
        lines = []
        for n in range(1, 301):
            lines.append(dot(n))
            lines += [f"{k}. Point number {k} of the annotation with enough words to be long." for k in (1, 2, 3)]
        self.assertEqual(len(self.numbers(lines)), 300)


class SweepTests(unittest.TestCase):
    """Systematic sweeps: real DOIs broken at every separator, and undated second lists."""

    DOIS = ("10.1109/CVPR.2016.90 10.1016/j.chb.2016.05.083 10.1037/0003-066X.59.1.29 10.1126/science.286.5439.509 "
            "10.1162/neco.1997.9.8.1735 10.1111/j.1365-2486.2009.02017.x 10.1111/j.1540-6261.1993.tb04702.x "
            "10.1609/aaai.v34i04.5953 10.31234/osf.io/abc12 10.3224/ijre.v7i1.04 10.1093/oso/9780198830641.003.0001 "
            "10.3389/fpsyg.2019.01234 10.48550/arXiv.2005.14165 10.17863/CAM.12345 10.1001/jama.2020.1585 "
            "10.1002/anie.201912345 10.1016/S0140-6736(20)30183-5 10.1007/978-3-658-44153-1_3").split()
    AFTER = ("", "\nSmith, J. (2020). Next.", " Retrieved from https://x.org", " [Zugriff am 12.03.2021]", " PMID: 31852123",
             " S. 12-15.", " Bd. 3.", " Jg. 12, H. 3.", " 2019.", " ISBN 978-3-16-148410-0", " 12(3), 45-67.", " p. 12.")

    def test_dois_broken_at_every_separator(self):
        for doi in self.DOIS:
            breaks = [i + 1 for i, ch in enumerate(doi) if ch in "./-_" and i + 1 < len(doi) and i >= doi.index("/")]
            for position in [None] + breaks:
                printed = doi if position is None else doi[:position] + "\n" + doi[position:]
                for after in self.AFTER:
                    text = f"Author, A. (2019). Title. Journal, 1(2), 3-4. doi:{printed}{after}"
                    self.assertEqual(find_doi(text), doi.lower(), text)

    def test_text_after_a_whole_doi_is_not_glued_on(self):
        for after in (". 12. Smith, J. (2020).", ".\n3. Müller, A. (2019).", ". 2019.", ". 3., überarb. Aufl.",
                      ". 120 S.", ". 12 p.", ". 10115 Berlin.", ". 2019, in press."):
            for doi in ("10.1109/CVPR.2016.90", "10.1016/j.chb.2016.05.083", "10.1109/5.726791"):
                self.assertEqual(find_doi(f"Author, A. (2019). Title. doi:{doi}{after}"), doi.lower(), doi + after)

    def test_undated_source_lists_and_appendices(self):
        def numbers(lines):
            return [m.group(1) for m in numbered_slots("\n".join(["References", *lines]), 0)]
        dated = lambda n: f"[{n}] Author{n} A, Coauthor B. A reasonably long title of work {n}. J Things. 2019;12(3):45-67."  # noqa: E731
        archive = lambda n: f"[{n}] Stadtarchiv Mannheim, Bestand Kleine Erwerbungen, Nr. {n}, Brief an den Rat der Stadt."  # noqa: E731
        item = lambda n: f"[{n}] How often do you use digital media in your teaching, and how satisfied are you?"  # noqa: E731
        self.assertEqual(len(numbers([dated(n) for n in range(1, 21)] + ["Primary sources"] +
                                     [archive(n) for n in range(1, 8)])), 27)
        self.assertEqual(len(numbers(["Quellen"] + [archive(n) for n in range(1, 8)] + ["Literatur"] +
                                     [dated(n) for n in range(1, 21)])), 27)
        self.assertEqual(len(numbers([dated(n) for n in range(1, 21)] + ["Appendix: questionnaire"] +
                                     [item(n) for n in range(1, 11)])), 20)


class RealReportTests(unittest.TestCase):
    """Defects seen in a real report (an engineering paper, 10 numbered references)."""

    def review(self):
        from src.evidence import SCHEMA_VERSION
        from src.evidence.analysis import analyze_artifact
        from src.evidence.review import build_review
        from tests.test_v2_requirements import CONFIG, reference

        def ref(n, title, authors, year, cited, status="resolved", external=None, issues=None):
            item = reference(f"r{n}", f"[{n}]", citation_count=cited, status=status, external=external,
                             parsed={"title": title, "authors": authors, "year": year})
            if issues:
                item["original_data"]["extraction_status"] = "needs_review"
                item["original_data"]["extraction_issues"] = issues
            return item
        refs = [ref(1, "Li -Fi Based Underwater Communication", ["Srikanth, B."], "2024", 1),
                ref(2, "Real -Time Audio Transmission Using Visible Light Communication",
                    ["Soh, M. Y.", "Ng, W. X.", "Teo, T. H."], "2018", 1,
                    external={"title": "Real-Time Audio Transmission Using Visible Light Communication",
                              "authors": ["Mei Yu Soh", "T. Hui Teo"], "year": 2018}),
                ref(3, "Experimental Demonstration Of 2 -Gbps Underwater Wireless Optical Communication",
                    ["Xiaojian Hong"], "2022", 1,
                    external={"title": "Experimental Demonstration of 55-m / 2-Gbps Underwater Wireless Optical "
                                       "Communication", "authors": ["Xiaojian Hong"], "year": 2022}),
                ref(4, "Low-cost tools", ["Doe, J."], "2020", 0),
                ref(8, None, ["Abdelrahman Elfikky"], None, 0, status="not_found",
                    issues=["bibliography_fragment", "bibliography_boundary_ambiguous"]),
                ref(9, "Underwater Visible Light Communication", ["Aly"], "2024", 1, status="not_found",
                    issues=["bibliography_boundary_ambiguous"]),
                ref(10, "Experimental Demonstration Of Underwater Optical Wireless Communication", ["Iqbal"], "2022", 0)]
        data = {"schema_version": SCHEMA_VERSION, "manuscript_metadata": {"title": "Paper — test", "authors": ["A B"]},
                "extraction_metadata": {"bibliography_ambiguities": [
                    {"kind": "possible_split_entry", "reference_ids": ["[8]", "[9]"]}]},
                "scored_references": refs}
        config = SimpleNamespace(**vars(CONFIG), SELF_CITATION_RATE_THRESHOLD=0.25, SELF_CITATION_MIN_COUNT=3,
                                 SELF_CITATION_CONCENTRATION=0.15)
        return build_review(analyze_artifact(data, config))

    def test_a_split_entry_is_diagnosed_with_the_likely_renumbering(self):
        review = self.review()
        split = [f for f in review["findings"] if f["category"] == "split_entry"]
        self.assertEqual(len(split), 2)
        self.assertIn("Entries [8] and [9] look like one reference broken in two", split[0]["explanation"])
        self.assertIn("the text's [9] probably means what is now [10]", split[0]["explanation"])
        unresolved = {f["reference_id"] for f in review["findings"] if f["category"] == "identity_unresolved"}
        self.assertNotIn("r8", unresolved)
        self.assertNotIn("r9", unresolved)

    def test_the_summary_counts_what_the_report_lists_and_says_what_did_not_run(self):
        from src.assistant.report import report_pdf
        review = self.review()
        overview = AuditAssistant(review).answer("summary")
        points = " ".join(overview["points"])
        self.assertIn("Listed but never cited in the text: 3 references", points)
        self.assertIn("could not be checked for any of the", points)
        pdf = report_pdf(review)
        self.assertNotIn(b"\x97", pdf.split(b"/Title", 1)[1][:200])             # the title is UTF-16, not cp1252

    def test_titles_and_authors_are_compared_precisely(self):
        review = self.review()
        details = " ".join(f["explanation"] for f in review["findings"] if f["category"] == "metadata_difference")
        self.assertIn("the database title also has “55-m”", details)
        self.assertNotIn("Teo", details)                                     # "T. Hui Teo" is Teo, T. H.
        # The record lists 2 of the 3 printed authors and both are printed: a short record, not an error.
        self.assertNotIn("Ng, W. X.", details)
        shorter = [f["explanation"] for f in review["findings"] if f["category"] == "author_list_shorter"]
        self.assertEqual(len(shorter), 1)
        self.assertIn("lists 2 of the 3 printed authors; it leaves out Ng, W. X.", shorter[0])
        assistant = AuditAssistant(review)
        self.assertEqual(assistant.by_id["r1"]["title"], "Li-Fi Based Underwater Communication")
        from src.assistant.engine import display_title
        self.assertEqual(display_title("Lehr- und Lernforschung"), "Lehr- und Lernforschung")


if __name__ == "__main__":
    unittest.main()
