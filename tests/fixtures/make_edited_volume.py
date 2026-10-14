"""Generate tests/fixtures/evidence/edited_volume.pdf (needs reportlab only to regenerate).

A small synthetic edited volume reproducing what real German and English
edited books do: one reference list per chapter, footnote citations
("Vgl. Wenger, 1998"), author names in capitals, "(o. J.)" dates, an
"Internetquellen" subsection, a figure list after a reference list, a part
divider page, APA narrative and parenthetical citations with year suffixes,
and a citation whose year disagrees with the list. All names and works are
invented except a few classics cited only by surname and year.

    python tests/fixtures/make_edited_volume.py
"""

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT = Path(__file__).parent / "evidence" / "edited_volume.pdf"

PAGES = [
    # 1: cover
    ["Anna Meier, Jonas Weber (Hg.)", "LEHREN UND LERNEN", "Ein Testband für CitePrism"],
    # 2: chapter 1 opening (German, footnotes)
    ["Anna Meier, Jonas Weber",
     "Netzwerke in der Hochschullehre",
     "Wie Communities die Lehre verändern",
     "Hochschulen arbeiten zunehmend in Netzwerken zusammen, um die Lehre gemeinsam",
     "weiterzuentwickeln und dabei Erfahrungen über die Grenzen einzelner Institutionen",
     "hinweg auszutauschen. Lernen in Gemeinschaften gilt als zentral für die Entwicklung",
     "von Lehrkompetenz und die Verbreitung guter Praxis in allen Fachbereichen.1 Solche",
     "Netzwerke verbinden Lehrende verschiedener Disziplinen und schaffen Räume für die",
     "gemeinsame Reflexion über Lehrformate, Prüfungen und digitale Werkzeuge im Alltag.",
     "Rückmeldungen der Lernenden wirken dabei besonders stark auf den Lernerfolg und die",
     "Motivation der Studierenden in Lehrveranstaltungen aller Größenordnungen.2",
     "1 Vgl. Wenger, 1998; Meier, 2021.",
     "2 Vgl. Hattie & Timperley, 2007."],
    # 3: chapter 1 continued + bibliography
    ["12",
     "MEIER, WEBER",
     "Erwachsenenbildung folgt eigenen didaktischen Prinzipien, die auch für die Lehre an",
     "Hochschulen fruchtbar gemacht werden können, wenn Lehrende ihre Rolle reflektieren.3",
     "Die Stiftung fördert entsprechende Netzwerke seit mehreren Jahren mit Programmen.4",
     "3 Vgl. Siebert, 2005.",
     "4 Vgl. Stiftung Hochschullehre, o. J.",
     "Literatur",
     "HATTIE, J. & TIMPERLEY, H. (2007). The power of feedback. Review of Educational",
     "Research, 77(1), 81–112. DOI: https://doi.org/10.3102/003465430298487.",
     "MEIER, A. (2021). Communities in der Hochschuldidaktik. Ein Überblick. Bielefeld:",
     "Transcript.",
     "SIEBERT, H. (2015). Didaktisches Handeln in der Erwachsenenbildung. Augsburg: Ziel.",
     "WENGER, E. (1998). Communities of practice. Learning, meaning, and identity.",
     "Cambridge: Cambridge University Press.",
     "Internetquellen",
     "STIFTUNG HOCHSCHULLEHRE (O. J.). Fokus Netzwerke. Abgerufen am 2. März 2025 von",
     "https://example.org/netzwerke.",
     "Abbildungen",
     "ABB. 1: Netzwerkstruktur. © Testband."],
    # 4: part divider
    ["II.", "Feedback und", "Bewertung"],
    # 5: chapter 2 opening (English, narrative and parenthetical)
    ["Lena Fischer-Hall, Tom Berger",
     "Feedback that works",
     "Lessons from a teaching network",
     "Feedback is among the most powerful influences on learning (Hattie & Timperley, 2007),",
     "yet students often struggle to act on comments they receive in their courses. Fischer-Hall",
     "(2024a) describes a peer review format that increased uptake, and a follow-up study by the",
     "same group extended it to large lectures (Fischer-Hall, 2024b). Experiential learning",
     "theory explains why reflection matters for this kind of practice (Kolb, 1984). Berger et",
     "al. (2022) caution that rubrics alone rarely change how students respond to feedback,",
     "and Carless and Boud (2018) argue that feedback literacy has to be developed deliberately",
     "through dialogue between teachers and students over time."],
    # 6: chapter 2 references
    ["24",
     "FISCHER-HALL, BERGER",
     "References",
     "Berger, T., Klein, S., & Novak, P. (2022). Rubrics and their limits in higher education.",
     "Assessment in Practice, 14(2), 33–47. https://doi.org/10.1234/aip.2022.14.33",
     "Carless, D., & Boud, D. (2018). The development of student feedback literacy: Enabling",
     "uptake of feedback. Assessment & Evaluation in Higher Education, 43(8), 1315–1325.",
     "https://doi.org/10.1080/02602938.2018.1463354",
     "Fischer-Hall, L. (2024a). Peer review in seminars. Journal of Teaching Practice, 3(1),",
     "1–12.",
     "Fischer-Hall, L. (2024b). Scaling peer review to lectures. Journal of Teaching Practice,",
     "3(2), 20–31.",
     "Hattie, J., & Timperley, H. (2007). The power of feedback. Review of Educational Research,",
     "77(1), 81–112. https://doi.org/10.3102/003465430298487",
     "Kolb, D. A. (1984). Experiential learning: Experience as the source of learning and",
     "development. Englewood Cliffs, NJ: Prentice Hall.",
     "Unused, A. (2019). A source that the chapter never cites. Some Journal, 1(1), 1–2."],
]


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(OUT), pagesize=A4)
    pdf.setTitle("Lehren und Lernen")
    pdf.setAuthor("Anna Meier, Jonas Weber (Hg.)")
    for lines in PAGES:
        y = 800
        for line in lines:
            pdf.setFont("Helvetica", 10)
            pdf.drawString(50, y, line)
            y -= 16
        pdf.showPage()
    pdf.save()
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
