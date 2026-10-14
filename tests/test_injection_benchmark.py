"""The error-injection benchmark, and the defects it exposed (5.4).

The benchmark plants known errors in a real document and counts what is found.
Run on the 263-page German edited volume, it exposed four real defects, each
fixed and pinned here:

* "K. Knorr Cetina" / "Knorr Cetina, K.", "P. Fel ten" (PDF letter spacing) and
  "M. O’Shannessy" were different people, so correct records were reported as
  having a different first author.
* Two different works by one author under one main title ("Medienkompetenz –
  Begrifflichkeit …", "Medienkompetenz: theoretisch …") were matched as the
  same work once subtitles were tolerated.
* One mistyped citation year was reported on every work by that author
  ("Blume, 2022" on Blume 2023, 2024, 2025 and i. E.).
* An in-press entry was preferred over the entry one year away.
"""

import re
import unittest
from pathlib import Path

from src.evaluation.injection import run
from src.evidence import bibliography as bib
from src.evidence.consistency import compare
from src.evidence.extraction import pdf_source
from src.evidence.identity import resolve_candidates, title_similarity
from src.evidence.self_citation import same_surname, surname_variants

FIXTURES = Path(__file__).parent / "fixtures" / "evidence"


class BenchmarkGateTests(unittest.TestCase):
    """The detectors keep these results on the bundled edited volume; a regression fails the build."""

    @classmethod
    def setUpClass(cls):
        cls.report = run(pdf_source(FIXTURES / "edited_volume.pdf"), manuscript_authors=["Anna Meier", "Jonas Weber"],
                         seed=7, rounds=3, per_round=10, drop_trials=10)

    def test_citation_year_typos(self):
        typos = self.report["citation_year_typos"]
        # Each eligible citation is planted once: the trials are distinct, not repeats.
        self.assertGreaterEqual(typos["planted"], 5)
        self.assertLessEqual(typos["planted"], typos["eligible_citations"])
        self.assertEqual((typos["recall"], typos["precision"]), (1.0, 1.0))

    def test_dropped_citations(self):
        dropped = self.report["dropped_citations"]
        self.assertGreater(dropped["trials"], 0)
        self.assertEqual((dropped["recall"], dropped["other_entries_lost"]), (1.0, 0))

    def test_metadata_differences(self):
        for name, values in self.report["metadata_differences"]["perturbations"].items():
            with self.subTest(perturbation=name):
                self.assertGreater(values["trials"], 0)
                if "no_difference_reported" in values:
                    self.assertEqual(values["no_difference_reported"], 1.0)
                else:
                    self.assertEqual((values["detected"], values["right_severity"],
                                      values["other_fields_also_flagged"]), (1.0, 1.0, 0.0))

    def test_identity_resolution_never_picks_a_wrong_work(self):
        for name, values in self.report["identity_resolution"]["cases"].items():
            with self.subTest(case=name):
                self.assertEqual(values["wrong_match_rate"], 0.0)
                if "right_match_rate" in values:
                    self.assertEqual(values["right_match_rate"], 1.0)

    def test_self_citation_names(self):
        result = self.report["self_citation"]
        self.assertEqual((result["same_person_found"], result["different_person_flagged"]), (1.0, 0.0))

    def test_the_same_seed_gives_the_same_numbers(self):
        again = run(pdf_source(FIXTURES / "edited_volume.pdf"), manuscript_authors=["Anna Meier", "Jonas Weber"],
                    seed=7, rounds=3, per_round=10, drop_trials=10)
        self.assertEqual(again, self.report)


class NameTests(unittest.TestCase):
    def test_multi_word_split_and_apostrophe_surnames(self):
        pairs = [("K. Knorr Cetina", "Knorr Cetina, K."), ("Karin Knorr Cetina", "Knorr Cetina, K."),
                 ("M.F.N. Omran Zailuddin", "Omran Zailuddin, M.F.N."), ("P. Fel ten", "Fel ten, P."),
                 ("Fel ten, P.", "Felten, P."), ("M. O’Shannessy", "O'Shannessy, M."),
                 ("A. B. van der Berg", "van der Berg, A. B."), ("Mònica Feixas", "Feixas, M.")]
        for left, right in pairs:
            with self.subTest(left=left, right=right):
                self.assertTrue(same_surname(left, right))

    def test_different_people_stay_different(self):
        for left, right in [("A. Rosenberg", "Berg, A."), ("Anna Meier", "Maier, A."), ("J. Smith", "Smithson, J.")]:
            with self.subTest(left=left, right=right):
                self.assertFalse(same_surname(left, right))
        self.assertEqual(surname_variants("Karin Knorr Cetina"), {"cetina", "knorrcetina"})

    def test_the_index_writing_names_given_first_is_no_metadata_difference(self):
        printed = {"title": "Epistemic cultures", "year": 1999,
                   "authors": ["Knorr Cetina, K.", "Fel ten, P.", "O’Shannessy, M."]}
        record = {"title": "Epistemic Cultures", "year": 1999,
                  "authors": [{"display_name": "Karin Knorr Cetina"}, {"display_name": "Peter Felten"},
                              {"display_name": "Mary O'Shannessy"}]}
        self.assertEqual(compare(printed, record), [])


class TitleTests(unittest.TestCase):
    def test_two_subtitles_under_one_main_title_are_two_works(self):
        self.assertLess(title_similarity("Medienkompetenz – Begrifflichkeit und sozialer Wandel",
                                         "Medienkompetenz: theoretisch erschließend und praktisch folgenreich"), .8)
        query = {"title": "Medienkompetenz – Begrifflichkeit und sozialer Wandel", "authors": ["Baacke, D."],
                 "year": 1996}
        other = {"id": "other", "title": "Medienkompetenz: theoretisch erschließend und praktisch folgenreich",
                 "authors": [{"display_name": "Dieter Baacke"}], "publication_year": 1999}
        self.assertIsNone(resolve_candidates([other], query)["selected_candidate_id"])

    def test_a_dropped_or_added_subtitle_is_still_the_same_work(self):
        full = "Bildung, Praxistransfer und Kooperation. Kompetenzentwicklung für die Hochschullehre in Netzwerken"
        self.assertGreaterEqual(title_similarity(full, "Bildung, Praxistransfer und Kooperation"), .8)
        self.assertGreaterEqual(title_similarity("Bildung, Praxistransfer und Kooperation", full), .8)
        self.assertGreaterEqual(title_similarity(full, full.replace(". ", ": ")), .99)


class NearestYearTests(unittest.TestCase):
    def entries(self, years):
        found = []
        for year in years:
            label = "i.E." if year == "i.E." else year
            raw = f"BLUME, D. ({label}). Ein Titel aus dem Jahr {year}. Bielefeld: Transcript."
            found.append(bib.parse_entry(bib.Entry(raw=raw, start=0, end=len(raw)), raw))
        return found

    def test_one_typo_is_one_finding_on_the_nearest_entry(self):
        entries = self.entries(["2023", "2024", "2025", "i.E."])
        labels = bib.labels_for(entries)
        text = "Wie Studien zeigen (Blume, 2022), wirkt das."
        mismatches = []
        found = bib.find_citations(text, 0, len(text), entries, labels, mismatches)
        self.assertEqual([m["entries"] for m in mismatches], [[0]])           # Blume 2023 only
        self.assertEqual([(f["entry"], f["year_mismatch"]) for f in found], [(0, True)])

    def test_a_tie_keeps_every_tied_entry_and_links_none(self):
        entries = self.entries(["2020", "2024"])
        text = "Siehe Blume, 2022."
        mismatches = []
        found = bib.find_citations(text, 0, len(text), entries, bib.labels_for(entries), mismatches)
        self.assertEqual(mismatches[0]["entries"], [0, 1])
        self.assertEqual(found, [])

    def test_a_far_year_among_several_works_is_reported_but_not_linked(self):
        entries = self.entries(["2010", "2024"])
        text = "Siehe Blume, 2018."
        mismatches = []
        found = bib.find_citations(text, 0, len(text), entries, bib.labels_for(entries), mismatches)
        self.assertEqual(mismatches[0]["entries"], [1])                      # 2024 is nearer than 2010
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
