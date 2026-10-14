# Measuring CitePrism

Two kinds of evidence are needed to say how well CitePrism works. This page reports the first and says
exactly what is still missing for the second.

| | What it measures | Ground truth | Status |
|---|---|---|---|
| **Error-injection benchmark** | Do the rule-based detectors find a planted error, and do they raise false alarms? | By construction: the error was planted | **Done (5.4)** — results below |
| **Human-annotated evaluation** | Real-world precision and recall of every finding, including relevance and support judgements | Two independent annotators per reference | **Not done.** Format, templates, metrics and calibration code exist (`src/evaluation/`); the labels do not |

## 1. Error-injection benchmark

```powershell
python scripts/benchmark_injected.py paper.pdf --authors "First Author" "Second Author" --json results.json
```

The script reads the PDF, then plants known errors in its own text and reference entries, runs the
detectors again, and counts what is found and what is falsely flagged. It needs no network, no model and
no API key; the same seed always gives the same numbers (`--seed`). `tests/test_injection_benchmark.py` runs
it on the bundled test volume on every build and fails if any number drops.

| Planted error | How | Correct outcome |
|---|---|---|
| Citation year typo | A year in an in-text citation is changed (2023 → 2024), only where no other work by that author is as close | A year conflict on exactly that citation, nothing else |
| Dropped citation | Every in-text citation of one entry is blanked | That entry, and only it, becomes "listed but never cited" |
| Metadata perturbation | A record is built from the printed entry, then one field is changed | The changed field is reported with the right severity; harmless variants report nothing |
| Identity decoys | The right record is offered among 5 decoys (same first author where possible), or left out; no DOI | The right record is chosen; with it absent, nothing is chosen |
| Self-citation forms | A manuscript author written as "Blume, D.", "D. Blume", "BLUME, D.", "Dana Blume" …; and as another person ("Blume, Q.", "Bluma, D.") | Same person found; different person never flagged |

### Results — *Hochschullehre im digitalen Zeitalter* (263 pages, German, 13 chapters)

248 references, 237 in-text citations, author–year style. Seed 7 (seeds 11 and 23 give the same rates), manuscript
authors "Dana Blume" and "Benno Volk". Each planted error is a distinct trial; nothing is counted twice.

| Detector | Trials | Found | False alarms |
|---|---|---|---|
| Citation year typos (each eligible citation once) | 50 | **100%** | **0** |
| Dropped citations | 36 | **100%** | **0** other entries lost |
| Metadata: year off by one / wrong year | 210 / 210 | 100% / 100%, right severity | 0 |
| Metadata: title typo / another work's title | 210 / 210 | 100% / 100%, right severity | 0 |
| Metadata: first author replaced / DOI mistyped | 210 / 102 | 100% / 100%, right severity | 0 |
| Metadata: database adds a subtitle | 120 | 100% reported as *minor* | 0 |
| Metadata: harmless case and name order | 210 | — | **0** (silent in 100%) |
| Identity: exact / subtitle added / names given-first | 217 / 121 / 217 | 100% right | 0 wrong |
| Identity: title with a typo | 217 | 99.5% right, 0.5% left for review | 0 wrong |
| Identity: right record absent | 217 | — | 0.5% (1 case, see below) |
| Self-citation: same person, 6 written forms each | 12 | 100% | — |
| Self-citation: different person | 6 | — | **0** flagged |

The one identity case: with the right record absent, "Feixas & Flepp (2024), *The Reflective Companion*"
was matched to "Feixas & Flepp (2024), *Reflective Companion: Ein Framework und Leitfaden …*" — same
authors, same year, the same main title. It is plausibly the same work under a fuller title; it is
counted as a wrong match because the benchmark cannot know.

### Defects the benchmark found (all fixed in 5.4)

The first run did not look like the table above. It found:

| Defect | Effect on real audits | Fix |
|---|---|---|
| "K. Knorr Cetina" / "Knorr Cetina, K.", "P. Fel ten" (PDF letter spacing) / "Felten, P.", "M. O’Shannessy" / "O'Shannessy, M." were different people | A correct record from OpenAlex (which writes given names first) was reported as having a different first author; self-citations by such authors were missed | One name parser for all checks: after initials, the whole rest is the surname; curly apostrophes normalised; given-first names keep every plausible multi-word surname |
| Two works under one main title ("Medienkompetenz – Begrifflichkeit …", "Medienkompetenz: theoretisch …", Baacke 1996 and 1999) matched as one | 5.3.1's subtitle tolerance could link a reference to the wrong work by the same author | A subtitle is ignored only when one side has none |
| One mistyped year reported on every work by that author ("Blume, 2022" on Blume 2023, 2024, 2025 and i. E.) | Four items for one typo | The conflict goes to the nearest entry (matching suffix first); ties keep every tied entry |
| An in-press entry preferred over the entry one year away | The wrong entry named | In-press counts as 1.5 years away |

### Independent review, four rounds

After the benchmark, a separate reviewer (an agent that had not seen this work) probed the 5.4 changes
with adversarial inputs. It reproduced 17 defects — three serious: numbered lists that lost entries after
a line such as "1. Aufl.", the surname "Ma" removed as if it were a degree, DOIs broken by a line break
being cut short. All were fixed; a second review of the fixes found seven new problems introduced by them
(among them a way to quote an accusation past the assistant's guard, and German volume numbers such as
"Heft 10. 2008/09" read as DOIs), which were fixed in turn. `tests/test_review_fixes.py` pins every one,
and the same review found that re-auditing a paper overwrote the earlier audit's results — each audit
now keeps its own copy.

A third round reproduced 12 further problems (among them accusations that got past the guard through a
translation, and self-citations missed for names with degrees), and a fourth round 9 problems that the
third round's fixes had introduced — German compound words such as "Wissenschaftsbetrug" passing the
guard, a short first list entry being dropped, and "Garcia Lopez MD" in a bibliography making every
"Gonzalo Lopez" a self-citation. A fifth round found 11 more, the most serious that the fourth round's
DOI rule cut real DOIs broken after a dot ("10.1109/CVPR." + "2016.90"). All are fixed and pinned by tests;
the benchmark numbers above are unchanged by them, and the book's 237 citations, 108 DOIs and 12 year
conflicts are identical to 5.3.1. A final systematic sweep breaks 62 real DOIs at every separator and adds 20
kinds of trailing text (9,680 cases); all are read whole (`SweepTests`).

### What these numbers do and do not show

- They show that the **rule-based detectors** (year conflicts, uncited entries, metadata comparison,
  identity ranking, name matching) do what they are designed to do on a real, messy, multilingual book,
  including its PDF artefacts.
- They do **not** measure relevance or support judgements (these need a model and human labels), the
  coverage of the scholarly databases (a work missing from every index cannot be matched), PDF reading
  of other layouts, or how often such errors occur in real manuscripts.
- Year typos are planted only in citations whose year appears in the located citation text; citations
  placed in footnotes are located at their superscript and are not covered by that test (52 of the
  book's 237 citations were eligible; 50 were planted).
- Numbered reference lists need the AI model to read their fields, so the metadata and identity tests
  cannot run offline on them.

## 2. Human-annotated evaluation — the next step

A defensible claim of accuracy needs labelled data. The plan, using what already exists:

1. **Public benchmark first.** Badalova & Mayr (GESIS, 2026) released 104 manually verified references
   from three documents (71 verified, 33 problematic) on Zenodo, doi:[10.5281/zenodo.21457492](https://doi.org/10.5281/zenodo.21457492),
   and report five tools at roughly 31–51% precision for 55–97% recall ([arXiv:2607.22693](https://arxiv.org/abs/2607.22693)).
   Running CitePrism on the same documents gives a direct, published comparison.
2. **Own multilingual set.** 30–50 manuscripts across at least four domains (including German-language
   humanities, where commercial tools are weakest); `scripts/make_annotation_template.py` produces blind
   templates; two annotators per reference; agreement reported (Cohen's κ) before any metric.
3. **Report** with `scripts/run_evaluation.py --markdown`: per-objective precision/recall with
   manuscript-level bootstrap confidence intervals, per-domain breakdown, and calibrated thresholds from
   `scripts/calibrate_thresholds.py` (it refuses to recommend thresholds from too little data).
