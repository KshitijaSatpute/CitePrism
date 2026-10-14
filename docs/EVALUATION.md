# CitePrism 5.4 — an examiner's evaluation

This is the assessment a demanding examiner would write after reading the code, running the tests and
asking whether CitePrism could be deployed and hold its own against established tools. It is based on
evidence in this repository — the test suite, the error-injection benchmark ([BENCHMARK.md](BENCHMARK.md)),
the requirements traceability ([V2_REQUIREMENTS_TRACEABILITY.md](V2_REQUIREMENTS_TRACEABILITY.md)) and five
rounds of independent review — and, for the market, on the sources listed at the end (checked October 2026).

## Verdict

**As an academic system, CitePrism is exceptional in scope and engineering.** It implements every objective
of the V2.0 brief — irrelevant references, questionable self-citation, metadata inconsistencies, possibly
missing literature, human oversight — with evidence a reviewer can verify, and adds what most research
prototypes lack: a review workflow with an audit trail, signed exports, role-based access, a durable job
queue, offline operation, and explanations a non-expert can follow.

**As a product, it is ready for a supervised pilot, not for sale.** The rule-based checks are measured and
reliable on a demanding real document; the AI judgements (does this source support this sentence? is it
relevant?) are carefully constrained but **not yet measured against human labels**, and that is the claim
any editor, funder or reviewer of a publication will ask about first. "Invincible" is not a standard any
research-integrity tool meets: a July 2026 benchmark found the best published reference checkers at roughly
half precision. The defensible position is different and stronger — *transparent, measured,
human-in-the-loop* — and CitePrism is close to being able to claim it.

## Scorecard

| Criterion | Rating | Evidence |
|---|---|---|
| Fit to the V2.0 brief | **Strong** | Every concept, problem, objective and method item traced to code and tests; the one gap is E6 (annotated evaluation), now partly met by the synthetic benchmark |
| Engineering quality | **Strong**, with a caveat | 585 tests (558 run without the web stack, 24 of them skip without PostgreSQL or live providers); typed contracts; CI gates; secret scanning. Caveat: five review rounds found 56 real defects in code that passed its tests, two runs on a real student paper found 16 more, and a sixth review found 11 in the first draft of those fixes — more certainly remain, and the HTTP and PostgreSQL suites must be run on a full installation |
| Rule-based detectors | **Strong** (synthetic) | On a 263-page German edited volume: 100% of planted year typos, dropped citations and metadata changes found, no false alarms; identity matching never picks a wrong work among decoys |
| AI judgements (relevance, support) | **Unmeasured** | Quotation-grounded, never shown without a located quote; thresholds are defaults until calibrated on labelled data |
| Explainability and auditability | **Strong** | Located quotations, score components, append-only decisions, hash-chained activity, signed exports; each audit now keeps its own immutable artifact |
| Usability for non-experts | **Good**, untested with users | Chat in everyday words (vague, multi-part, typos, German), simple-language register, PDF report with a to-do list, a courteous letter to the authors; no usability study or accessibility audit yet |
| Privacy and compliance | **Adequate** | Upload screen names where manuscript text goes; `local_only` enforced; no DPIA, retention policy or processor agreements yet; the signing key committed earlier must still be rotated |
| Coverage | **Adequate** | Strong for DOI-registered journal literature; weak for German books, grey literature and websites (no library-catalogue lookup) |
| Scale and operations | **Unproven** | Queue, workers and a load-test script exist; no capacity measured against a deployment |

## How it compares

| Tool | What it checks | Strength | Where CitePrism stands |
|---|---|---|---|
| Springer Nature irrelevant-reference checker (in production since April 2025) | Irrelevant references in submissions to almost all its journals and books; alerts its research-integrity group | Publisher-scale data and production validation | Comparable aim; CitePrism shows *why* (quotes, scores) and is available to anyone, but is unvalidated at scale |
| scite (Smart Citations, Reference Check) | Retractions, editorial notices and supporting/contrasting citation statements for the works a manuscript cites | 1.6 billion classified citation statements, 317 million articles | Cannot match the index; CitePrism checks the manuscript's *own* sentences against the source, which Reference Check does not |
| Hallucinated-reference checkers (RefChecker, CheckIfExist, Hallucinator, HalluCiteChecker, HalRef) | Whether cited works exist | Simple, fast, open | GESIS benchmark (2026): roughly 31–51% precision at 55–97% recall. CitePrism's identity checks do this and more; it should be run on the same benchmark |
| Integrity Manager — References Validity Check (Molecular Connections) | Hallucinated references at submission | Claims higher accuracy than the best tool in the published benchmark (small dataset) | Same task; CitePrism adds relevance, self-citation and missing literature |
| Edifix (Inera) | Reference formatting, linking and data mismatches against Crossref and PubMed | Mature production tool for publishers | Overlaps on metadata differences; CitePrism is not a copy-editing tool |

**On "the US government, NASA or Google".** None of them sells a citation-audit product. What they run —
Google Scholar, NASA ADS, NIH's PubMed and iCite — are *indexes*: the data such tools are built on.
CitePrism cannot match their scale and does not need to: it builds on open indexes (OpenAlex, Crossref,
DataCite and others), which is also a dependency to manage.

**Where CitePrism can lead:**

1. *Evidence you can check.* Every AI statement carries a quotation that was found in the source; nothing
   is shown as a verdict.
2. *Data sovereignty.* It runs entirely on an institution's own server with a local model — important in
   the EU and for confidential manuscripts.
3. *Multilingual humanities.* German author–year bibliographies, footnote citations ("Vgl. …"), edited
   volumes split into chapters — exactly where publisher tools are weakest.
4. *People who are not specialists.* Plain-language answers, a printable to-do list, a neutral letter to
   the authors.
5. *Openness.* Code, benchmark and limitations are public and reproducible.

**Where it cannot compete today:** index scale, validated accuracy, integration with editorial systems
(ScholarOne, Editorial Manager, OJS), certifications (ISO 27001), and support.

## What 5.4 changed

- **A real conversation.** The assistant understands vague, multi-part messages in everyday words — the
  test case is *"I got the results and I can see some actions but I don't know what to do with them … explain
  me like a 5 year old … give me in pdf the finds with recommended steps … and lacking citations"* — keeps
  the conversation ("why?", "the second one", "explain it more simply", "yes please"), answers "is this paper
  any good?" honestly, drafts a letter to the authors, declines to call anyone dishonest, and makes a
  plain-language PDF report. It works without any AI model; a model only phrases what the audit found and
  is checked before anything is shown.
- **Measurement.** An error-injection benchmark with ground truth by construction; its first run exposed four
  real defects (multi-word surnames, two works under one main title, one typo reported several times, an
  in-press preference).
- **Independent review, five rounds.** A reviewer that had not seen the work probed it adversarially and
  reproduced 17, then 7, then 12, then 9, then 11 problems — among them numbered lists losing entries, DOIs truncated or
  invented, ways to quote an accusation past the assistant's guard, and a later audit of the same paper
  overwriting the earlier audit's results. All were fixed and each is pinned by a test
  (`tests/test_review_fixes.py`).
- **Systematic sweeps.** 62 real DOIs from 47 publishers and registries, each broken by a line break at every
  separator and followed by 20 kinds of trailing text (9,680 cases), are all read whole; numbered lists with
  undated entries, archival source lists, appendices and heavy line noise keep exactly their entries.
- **A real paper, checked by hand (5.4.1, 5.4.2).** A bachelor's engineering paper with 10 numbered references
  was audited and every item compared with the publishers' records. Run 01a12009 was right on every finding
  but one — a wrong year on two references, a missing "55-m /" in a title, four uncited entries, one entry
  split in two by Word's numbering (with the likely shift of the text's numbers), a claim its source's
  abstract does not back up — and called two genuine co-authors a difference. 5.4.2 fixes that and what the
  run left unfinished: a dblp outage no longer stops three references at "not checked" when five other
  databases answered, a model reply in the wrong shape is asked again once, and the summary sorts every cited
  reference into exactly one outcome.
- **Data handling.** The upload screen no longer claims the paper "stays on the server": it names the model
  services that receive the text and warns before confidential uploads. `CITEPRISM_MODEL_DATA_TERMS=local_only`
  is enforced.

## Before deployment, in this order

| # | Step | Why | Effort |
|---|---|---|---|
| 1 | Rotate the export-signing key that was committed to the repository; run `verify_phase4.ps1 -WithPostgres`; run `pip-audit` and `npm audit` | Signed exports are only as trustworthy as the key; the HTTP and PostgreSQL suites cannot run in the development sandbox | Hours |
| 2 | Use a paid model plan whose terms exclude training (with a data-processing agreement) or a local model; set `CITEPRISM_MODEL_DATA_TERMS`; write a short DPIA, privacy notice and retention rule | Free model plans may keep and read submitted text; manuscripts under review are confidential | Days |
| 3 | Run CitePrism on the public GESIS benchmark (104 verified references, Zenodo) and report against the five published tools | The first independent, comparable accuracy number | Days |
| 4 | Annotate 30–50 manuscripts in at least four fields (two annotators, agreement reported); calibrate relevance thresholds with `scripts/calibrate_thresholds.py` | Measures the AI judgements, the headline claim | Weeks |
| 5 | Add library-catalogue lookups by ISBN and title (e.g. Deutsche Nationalbibliothek, Open Library) | Books are most of the unresolved references in humanities manuscripts | Days |
| 6 | Usability test with 5–8 people without technical background (task-based, SUS score); WCAG 2.2 AA review | Evidence that the plain-language design works for its audience | Days |
| 7 | Load test against a deployment; backups and restore drill; monitoring | Capacity and recovery are claims only once measured | Days |
| 8 | An OJS plugin as the first editorial integration | Open Journal Systems is widely used by university and society journals | Weeks |
| 9 | Write up steps 3–4 for publication | The brief's publication goal; the benchmark and annotations are the contribution | Weeks |

## Known limitations

- Relevance and support judgements are unmeasured (step 4).
- Numbered reference lists need the AI model to read their fields; author–year lists are read by rule.
- The year-typo benchmark covers citations whose year appears in the located text; footnote citations are
  located at their superscript.
- The assistant's count check covers English phrasing; German model answers are checked for numbers and
  accusations but not for which topic a count belongs to.
- The accusation guard is strict by design and can reject harmless text ("abgeschrieben" for a written-off
  asset, "the manipulability of voting rules"); the reader then gets the built-in answer instead.
- An all-capitals name such as "JEAN MBA" is ambiguous with the Vancouver form "SMITH JA".
- A bare number printed right after a DOI that ends in a dot ("doi:10.1000/abc. 31852123") is read as part of
  the DOI, because real DOIs broken at a dot look exactly so ("10.1109/5." + "726791"); the doi.org check then
  reports a wrong DOI as unregistered, so a reviewer sees it. A DOI broken inside a run of letters and digits
  with no dot or hyphen ("gkz" + "1021") is cut at the break.
- A numbered list after the bibliography (an appendix) is read as a second reference list, like "Further
  reading".
- The pipeline still writes one scored file per document; each audit now copies its own, but two audits
  of the same paper finishing at the same moment could still race.

## Sources

- Springer Nature, *New AI tool to identify irrelevant references* (7 April 2025):
  https://group.springernature.com/de/group/media/press-releases/new-research-integrity-ai-tool/27769148
- Badalova & Mayr, *Detecting Hallucinated and Suspicious Citations: What Current Tools Can and Cannot Do*
  (arXiv:2607.22693, July 2026); dataset doi:10.5281/zenodo.21457492: https://arxiv.org/abs/2607.22693
- CASRAI, *Hallucinated citation detection tools: what they can and cannot do* (August 2026):
  https://casrai.org/news/hallucinated-citation-detection-tools-what-they-can-and-cannot-do-2026
- Boston University, *Stringhini's open-source tool helps peer reviewers catch hallucinated citations*
  (June 2026): https://www.bu.edu/eng/2026/06/17/fictional-bibliographies/
- Molecular Connections, *Hallucinated references are a growing industry crisis* (August 2026):
  https://molecularconnections.com/blog/hallucinated-references-are-a-growing-industry-crisis.-here-s-how-we’re-solving-it
- Educators Technology, *scite review* (September 2026): https://www.educatorstechnology.com/2026/09/scite-review.html
- Inera, *What is Edifix?*: https://support.inera.com/support/solutions/articles/1000201380-what-is-edifix-
- Google, *Gemini API Additional Terms of Service* (last updated 28 April 2026): https://ai.google.dev/terms
