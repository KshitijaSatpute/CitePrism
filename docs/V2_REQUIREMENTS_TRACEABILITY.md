# CitePrism V2.0 — requirements traceability

Release `5.4.2` (5.1 closed the V2.0 objectives; 5.2 added reliability,
coverage, calibration and the plain-language assistant; 5.3 makes parsing work
for author–year, footnote and multi-chapter documents — see M1; 5.3.1 fixes what a first chapter run exposed; 5.4 adds the conversational assistant, the plain-language report, the error-injection benchmark and the data-handling policy). This document maps every statement in the *CitePrism V2.0*
concept brief to the code that implements it and the test that proves it. It
was produced by auditing the Phase 4 system against the brief line by line, so
it also records what was **not** met before this release and what is still not
met now.

Status vocabulary:

- **Met** — implemented, wired into the audit, visible to reviewers, and tested.
- **Met, with stated limits** — implemented and tested; a limit is named below.
- **Infrastructure only** — the machinery exists; the outcome needs work that
  software cannot do (for example, human annotation).

All test references are offline: provider traffic is simulated and labelled.
`tests/test_v2_requirements.py` is the acceptance suite for this document.

## How the audit was done

1. Every sentence of the brief was turned into a numbered requirement.
2. Each was traced through the code to where a reviewer would see it.
3. A requirement counted as met only if a reviewer would actually see the
   result in the review queue, the evidence view, an export or the API.

That third rule mattered. Several capabilities existed in code but never reached
a reviewer, and one was silently disabled by default.

## Findings before this release

| # | Requirement | Phase 4 state | Cause |
|---|---|---|---|
| A | Hybrid relevance scoring (LLM + embeddings) | **Not met** | Phase 2 removed the numeric score in favour of grounded claim checks. `RS_llm` and `RS_final` were always `null`; embeddings were off by default. |
| B | Identify potentially irrelevant references | **Not met** | The assessment vocabulary had no way to say a source is *unrelated* to a claim, so irrelevance could never be reported. |
| C | Detect questionable self-citation patterns | **Not met** | Self-citation was computed per reference on surnames, then never shown in the review queue. No pattern detection, no ORCID. |
| D | Highlight metadata inconsistencies | **Partial** | Only year and DOI were compared. |
| E | Surface possibly missing key literature | **Partial** | Three unverified language-model guesses, which can name papers that do not exist. |
| F | Extension: retrieval-augmented verification of suggestions | **Not met** | — |
| G | Extension: ORCID-based author disambiguation | **Not met** | — |
| H | Extension: stronger offline deployment | **Not met** | Every stage required a hosted API. |

### Defects found in Phase 4 code during the audit

These were reliability bugs, not missing features. Each now has a regression test.

| Defect | Effect | Fix | Test |
|---|---|---|---|
| Multi-provider enrichment discarded OpenAlex and Semantic Scholar abstracts | Resolved references were left with no source text, so they were never assessed. With the default configuration this affected most references. | Resolution now returns the merged provider record behind the selected candidate. | `EnrichmentTests.test_merged_abstracts_survive_multi_provider_resolution` |
| The stage-reuse check compared against the OpenAlex-only version string | Every audit re-ran enrichment **and all model scoring**, defeating Phase 2 checkpoints and multiplying provider cost. | The orchestrator asks the selected enricher which version it writes. | `EnrichmentTests.test_the_orchestrator_expects_the_version_the_enricher_writes` |
| The lookup cache key ignored the provider set | An older OpenAlex-only result blocked Crossref and Semantic Scholar for up to 7 days. | The key includes providers, full-text and Unpaywall settings. | `EnrichmentTests.test_cache_key_includes_the_provider_set` |
| Full text was truncated to its first 16,000 characters and described to the model as an abstract | "Full-text evidence" was mostly the paper's introduction. | Passages are selected per citation by BM25 relevance to the citing sentence; the prompt names the source kind. | `PassageSelectionTests` |
| `.gitignore` did not exclude `secrets/` | The export signing key generated during setup was committed to the shared repository. | `secrets/`, key files and `.env.*` are ignored; CI and the verify script fail if any are tracked. | `scripts/check_no_secrets.py` (CI job `secrets`) |
| CI installed only the Phase 3 requirements | Phase 4 tests depend on packages that set does not include. | CI installs `requirements-phase4.txt`. | `.github/workflows/ci.yml` |
| One overloaded or retired model stopped the whole audit (5.2) | The default `gemini-2.5-flash` is being discontinued (2026-10-16) and answered HTTP 503 or 404 for many accounts; three attempts in ~15 s, then the audit failed. | Default moved to `gemini-3.6-flash`; fallback chain across models and providers (`LLM_FALLBACK_MODELS`; Gemini falls back to `gemini-3.1-flash-lite`, then `gemini-3.5-flash`); a retired model (404) is skipped like a busy one; OpenAI-compatible requests capped for free-tier token limits; the artifact records which model produced the parse. | `test_provider_fallback` |
| A correct arXiv or Zenodo DOI was reported as unregistered (5.2) | Those DOIs are registered with DataCite, which no index consulted; the reference became *unresolved* with a **high**-priority "may not exist" item. | DataCite joins the supplementary tier and the DOI-registration check. | `SupplementaryTierTests.test_a_correct_arxiv_doi_is_no_longer_reported_as_unregistered` |
| The library's open count mixed units (5.2) | It subtracted decisions (per finding) from references with any finding, including notes for information. | `attention_references` counts references with something to look at; the library, review header and assistant use the same definition. | `AnswerTests.test_summary_counts_match_the_review` |
| Author–year and German bibliographies were not read (5.3) | Found by running a real 263-page German edited volume: only English headings were recognised, the *last* heading was taken as the only bibliography, only numbered entries were anchored, and footnote citations were not detected. Result: 18 garbled references out of 248, none identified, 72 repetitive items. | Deterministic multilingual reader for author–year lists (`src/evidence/bibliography.py`): every reference list in the document, entries with authors, year (incl. *o. J.*, *i. E.*, suffixes), title, DOI; citations in text, parentheses and footnotes linked by surname and year; footnote citations attributed to the superscript in the sentence. On that book: 248/248 entries read, 225 linked to 304 citations, 12 citation-versus-list year conflicts found. | `test_author_year` |
| A failed reading looked like a normal audit (5.3) | Dozens of routine items hid that nothing had been read. | Reliability gate: a high-priority *extraction_reliability* item and an honest assistant summary when most entries are unreadable, citations are missing or nothing resolves. | `ReviewTests.test_an_unreadable_document_is_flagged_first_and_summarised_honestly` |
| References with a correct printed DOI ended as “Source provider unavailable” (5.3.1) | Found on chapter 1 of the German volume: Crossref stores a book's subtitle separately, so the printed “Title. Subtitle” looked like a different title; one DOI named chapter 3 of the cited book; and a rate-limited Semantic Scholar then turned “no confident match” into “provider unavailable”, a technical item that hid the real result. | Titles compared with and without subtitles and Crossref subtitles joined; a chapter DOI of the cited book is accepted and reported as a minor DOI detail; a printed DOI that names a different work is reported as a manuscript finding (*The printed DOI belongs to a different work*), never as an outage; outages name the database and reason; without an API key Semantic Scholar is asked only for references the other indexes cannot resolve. | `test_book_references` |
| Unclear metadata and assessment wording (5.3.1) | “authors: manuscript mcdermott; source None”, a repository (“CERN Document Server”) reported as a venue mismatch, and an unexplained “Assessment requires another attempt”. | Differences are sentences naming the printed authors missing from the record; repository venues are not compared; every assessment error code has a plain explanation and is shown. | `WordingTests` |
| Chapters of one book were indistinguishable (5.3.1) | Split chapters were titled “BOOK TITLE — n. chapter”, truncated to the same text in the library, and their files were named after the stored hash. | Chapter first (“2. Chapter title — Book title”), capitals softened, file names derived from the uploaded name. The review header counts references to look at, as the library does, and shows unfinished checks separately. | `ChapterTests`, `test_a_book_is_detected_and_split_into_chapters` |
| Multi-word, split and curly-apostrophe surnames were different people (5.4) | Found by the error-injection benchmark: "K. Knorr Cetina" vs "Knorr Cetina, K.", "P. Fel ten" (PDF letter spacing) vs "Felten, P.", "M. O’Shannessy" vs "O'Shannessy, M." — correct records from OpenAlex were reported with a different first author; self-citations by such authors were missed. | One name parser: after initials the rest is the surname; apostrophes normalised; given-first names keep every plausible multi-word surname. Used by identity, metadata and self-citation alike. | `NameTests` |
| Two works under one main title matched as one (5.4) | 5.3.1's subtitle tolerance matched Baacke 1996 "Medienkompetenz – Begrifflichkeit …" to Baacke 1999 "Medienkompetenz: theoretisch …". | A subtitle is ignored only when one side has none. | `TitleTests` |
| One mistyped year flagged on every work by that author (5.4) | "Blume, 2022" produced four items (Blume 2023, 2024, 2025, i. E.). | The conflict goes to the nearest entry (matching suffix first); ties keep all tied entries; linked only when one year away. | `NearestYearTests` |
| Extraction version not bumped after the 5.4 hardening (5.4) | Re-running an audit reused the earlier parse, so the hardening did not reach existing audits. | `page-anchors-v4.2`, `identity-v5`, `metadata-consistency-v5`. | `src/evidence/__init__.py` |
| The upload screen said the paper "stays on the CitePrism server" (5.4) | Its text is sent to the language-model provider; on free plans the provider may keep it (Gemini API terms for unpaid services forbid submitting confidential information). | The screen names the services and warns before confidential uploads; `CITEPRISM_MODEL_DATA_TERMS` records the arrangement, and `local_only` is enforced at start-up. | `test_data_policy` |
| Found by an independent review of 5.4 (5.4) | Numbered lists lost entries after an edition or issue line ("1. Aufl.", "(1) 23-45") or when extracted out of order; the surname "Ma" was removed as a degree; DOIs broken after "." were truncated, German volume numbers ("Heft 10. 2008/09") read as DOIs; counts ignored reviewers' decisions; "nothing missing" was claimed when the check had not finished; `local_only` could be bypassed through automatic similarity or an IPv6 address; a busy index forced a full re-assessment; the guard could be passed by quoting an accusation; re-auditing a paper overwrote the earlier audit's artifact. | Each fixed; every audit keeps its own artifact copy; the guard exempts only whole quoted titles and checks that a stated count is that topic's count. | `test_review_fixes`, `test_phase3_workspace.…earlier_results_alone` |
| Found by the fourth review round (5.4) | Fixes from the third round introduced or left: German compound fraud words ("Wissenschaftsbetrug") passed the guard; a short first list entry ("[1] WHO (2020).") was dropped; edition lines and numbered sub-points became entries, and long noisy lists took minutes; "Garcia Lopez MD" in a bibliography made every "Gonzalo Lopez" a self-citation; "this paper" was taken to name a reference; true counts ("7 matched, 1 not found") were rejected while "5 out of 8" passed; DOIs absorbed dates, versions and postcodes. | The guard reads compounds and new disguises and lets topics ("fraud detection") through; lists accept a run inside one entry never, and join runs in linear time; degree readings apply only to the manuscript's own authors; a title must be named inside the pointing phrase; each count is checked against its nearest claim; DOI numbers continue only where a registry numbers its DOIs. | `test_review_fixes.FourthReviewTests` |
| Found by the fifth review round (5.4) | The fourth round's DOI rule cut real DOIs broken after a dot ("10.1109/CVPR." + "2016.90"); given names that are also surnames ("Leon Smith, M.D.") lost self-citation matches; quoted screen labels ("possibly irrelevant") lost the question; topic words could carry an accusation ("This paper is fake news"); scores were read as counts and true counts rejected; a question after an accusation was dropped; titles in »…« quotes were missed; report requests and accusation replies fired too often. | DOI continuations follow publisher and registry numbering (journal codes, article numbers, mixed codes); only double surnames skip the degree reading; topics are exempt only where named as subjects and never in sentences about the authors; each count belongs to the claim in its own phrase or clause; every question in a message is answered; list runs continue only with entries of the same kind (dated references, not questionnaire items). | `test_review_fixes.FifthReviewTests` |
| Found in a real report (5.4.1) | A reference broken in two by Word's automatic numbering was reported as three unrelated items (incomplete entry, not found, never cited); the summary counted 2 metadata differences where the report listed 4; a title difference was cut off before the differing words; nothing said that no citation had been checked for relevance; the PDF's title showed “Š” for a dash; “T. Hui Teo” was not matched to “Teo, T. H.”. | One “split entry” item names both entries and, when the text cites the second but never the next, the likely renumbering; summary and report count the same items; title differences name the words; the summary states how many cited references were assessed; UTF-16 document title; the last name of a given-first name is always a surname variant. | `test_review_fixes.RealReportTests` |
| Found in the re-run of that report (5.4.2) | Four checks stayed unfinished: dblp failed for three references although five other databases had answered, and one model reply in the wrong shape was discarded without a second try; two genuine co-authors were reported as a difference because the database record lists fewer authors; the web app showed PDF spacing (“Li -Fi”, “Equa lizer”), clipped explanations and “differ by 4.; Venue”; numbered markers read like a list count; “2 of 2 scored references look clearly relevant” stood beside “does not clearly back up the claim”; the entry after a split pair was called uncited although the split note said the text's number probably meant it. | A failure of a specialist index (dblp, Europe PMC, DataCite) leaves the search *incomplete*, named in the finding, while a general-index outage still withholds the verdict, and doi.org is still asked about a printed DOI; dblp falls back to its official mirror after any failure (a 403 is named as a likely firewall or proxy block, not a key problem); HTTP status is recorded; OpenAlex is named as searched only when it is enabled; a reply in the wrong shape is asked again once on its own (failed quotations never are); a record that lists only some of the printed authors, all of them printed, is a note; a generic venue (“IEEE Conference”) is named as such, abbreviated real names (“Proc. IEEE”) are not; titles shown as printed; full explanations; “[6]” markers; one coverage sentence that sorts every cited reference into exactly one outcome; split pairs and the entry after them point at each other. | `test_real_report_542` (including `IndependentReviewTests`: 11 defects a sixth independent review found in the first draft) |
| PDF exports showed “?” for quotes, dashes and bullets | Text was encoded as Latin-1 while the fonts declare Windows-1252. | Windows-1252 encoding with transliteration; findings grouped per reference; notes for information summarised. | `src/workspace/exports.py` |
| The self-citation view described the pre-5.1 surname method | Reviewers were told distinct people sharing a surname are counted together, which ORCID and initial matching had made untrue for the counts. | Guidance distinguishes the counted matches from the surname-only chart. | `src/webapi/insights.py` |

## Traceability matrix

### Concept

| ID | Statement in the brief | Status | Implementation | Evidence |
|---|---|---|---|---|
| C1 | LLM-driven academic citation auditing | Met | `pipeline_orchestrator.py` stages 1–6 | `test_evidence.test_full_pipeline_…` |
| C2 | Transparent, scalable and explainable | Met | Quotation-grounded proposals, provider call records, trust ledger; durable queue and workers | `test_assessment_response`, `test_phase4_workspace` |
| C3 | Supports editors, reviewers and researchers | Met | Six RBAC roles, assignment, organizations; **5.2:** a guided *Ask CitePrism* mode for readers without technical background (plain-language summary, questions, references linked to evidence) beside the *Review in detail* workspace | `test_phase4_runtime.RbacTests`, `test_assistant` |
| C4 | Analyses citation relevance | Met (5.1) | `src/evidence/relevance.py` | `HybridRelevanceTests` |
| C5 | Analyses risk signals | Met | `build_review` findings; retractions; metadata; self-citation | `test_evidence`, `MetadataConsistencyTests` |
| C6 | Analyses possible literature gaps | Met (5.1) | `src/evidence/literature_gaps.py` | `LiteratureGapTests` |
| C7 | Human-in-the-loop workflow | Met | Append-only decisions, corrections, assignment, repairs | `test_phase3_workspace`, `test_phase4_workspace` |

### Problem statement

| ID | Problem the system must address | Status | How |
|---|---|---|---|
| P1 | Irrelevant citations | Met (5.1) | Objective O1 |
| P2 | Excessive self-citation | Met (5.1) | Objective O2 |
| P3 | Missing foundational work | Met (5.1) | Objective O4 — co-citation analysis is built for exactly this case |
| P4 | Weak traceability in citation decisions | Met | Superseding decisions, SHA-256 activity chain, immutable trust ledger, signed exports |
| P5 | Automated approaches lack contextual understanding | Met | Each citation use is assessed at its own marker in its own sentence; claims must be quoted at the target marker |
| P6 | Automated approaches lack explainability | Met | Every finding carries an explanation, the evidence quotes, and the score components |

### Objectives

| ID | Objective | Status | Implementation | Evidence |
|---|---|---|---|---|
| O1 | Identify potentially irrelevant references | **Met (5.1)** | New `unrelated` decision; hybrid relevance; `potentially_irrelevant` review item. Topic distance alone never labels a reference irrelevant. | `HybridRelevanceTests` |
| O2 | Detect questionable self-citation patterns | **Met (5.1)** | `src/evidence/self_citation.py`: tiered identity (ORCID / name+initial / surname-only, never counted); patterns for rate, author concentration, uncited and low-relevance self-citations; per-reference `questionable_self_citation` and manuscript-level `self_citation_pattern` items | `SelfCitationTests` |
| O3 | Highlight metadata inconsistencies | **Met (5.1, extended 5.3)** | `src/evidence/consistency.py`: DOI (mismatch and *unregistered*), title, first author, author list, year (off-by-one is minor), venue (abbreviation- and acronym-aware). **5.3:** citation-versus-bibliography year conflicts ("cited as Siebert, 2005; listed as 2015"), found without any external lookup. An unregistered DOI on a reference that matches no work is a **high** priority item — the signature of a fabricated reference. | `MetadataConsistencyTests` |
| O4 | Surface possibly missing key literature | **Met (5.1)** | Co-citation analysis: works cited by several of the manuscript's own references but not by the manuscript, resolved to real records. Model leads verified against the indexes. | `LiteratureGapTests` |
| O5 | Preserve human oversight | Met | No automated verdicts; every finding is a review item; decisions require a rationale | `test_phase3_workspace` |
| O6 | Scalable | Met, with stated limits | Durable PostgreSQL queue with `SKIP LOCKED` leasing; stateless workers; 4-way rate-limited enrichment. **5.2:** `scripts/load_test.py` measures latency percentiles and error rate under concurrent reviewers and gates on limits. **Limit:** it has not yet been run against a deployment, so no capacity figure is claimed. | `test_phase4_postgres` (needs PostgreSQL), `LoadTestSummaryTests` |

### Method

| ID | Stage | Status | Implementation | Evidence |
|---|---|---|---|---|
| M1 | Manuscript parsing | **Met (5.3), with stated limits** | Numbered lists: model extraction anchored to printed entries. Author–year lists (APA, Harvard, German conventions; headings in English, German, French, Spanish, Italian, Dutch, Swedish): read by rule, with the model refining titles where available; if every model is unavailable, the audit continues on the rule-based reading and says so. Footnote citations attributed to the citing sentence. Edited volumes and proceedings: each chapter's list is matched only with its own text, and the PDF can be split into one manuscript per chapter so authors and self-citation are per chapter. OCR for scanned pages. **Before 5.3 this row overstated coverage:** author–year and non-English bibliographies were not read. **Limits:** numbered lists still need a model; unusual styles (e.g. Chicago notes-bibliography with full citations only in footnotes, or law citation styles) are not read by rule; chapter titles are best-effort from page layout. | `test_reference_labels`, `test_phase4_evidence`, `test_author_year` |
| M2 | Reference metadata enrichment | Met | OpenAlex, Crossref, Semantic Scholar, merged on DOI; open-access full text; **5.2:** dblp, Europe PMC and DataCite for references the primary indexes cannot resolve | `EnrichmentTests`, `test_coverage_providers` |
| M3 | **Hybrid relevance scoring using LLM judgment and sentence embeddings** | **Met (5.1), with stated limits** | `RS_final = LLM_WEIGHT·RS_llm + EMBEDDING_WEIGHT·RS_embed` (0.6/0.4 by default, from configuration). `RS_llm` uses only quotation-grounded proposals. `RS_embed` uses sentence embeddings (`sentence_transformers` locally or `hf_inference`). **Limits:** (1) thresholds are defaults until calibrated on labelled data — 5.2 adds manuscript-level cross-validated threshold selection and `scripts/calibrate_thresholds.py`, which reports held-out precision/recall/F1 and refuses to recommend from fewer than 10 manuscripts or 20 irrelevant labels; (2) without `requirements-embeddings.txt` or an HF token, the similarity falls back to lexical TF-IDF, which is labelled as such — install the embeddings extra to meet the brief literally. | `HybridRelevanceTests`, `SimilarityBackendTests` |
| M4 | Risk detection | Met | Review policy v4 | `test_evidence` |
| M5 | Missing-citation suggestion | Met (5.1) | Model leads (Full audit) plus verification plus co-citation | `LiteratureGapTests` |
| M6 | Evidence snippets, rationales, interactive diagnostics | Met | Located quotations; rationales; score components; React evidence, insight and self-citation views. **5.2:** an audit assistant answers questions from the audit's own results — ten built-in topics with no model call, and optionally a model that may rephrase only those facts and cite only references that exist | `test_assessment_response`, `InsightPayloadTests`, `test_assistant` |

### Potential improvements and extensions

| ID | Extension | Status | Implementation | Evidence |
|---|---|---|---|---|
| E1 | Efficiency and response time: optimized pipeline, caching, batched inference, lightweight models | **Met (5.1)** | Stage reuse fixed (see defects); version- and provider-keyed lookup cache; per-thread concurrent enrichment with per-provider token buckets; batched assessment (5 uses per call), batched embedding encode, batched OpenAlex fetch (50 works per call); lexical similarity and local small models as lightweight options; analysis layer re-runs without model calls. Stage timings are recorded as telemetry. | `EnrichmentTests`, `RateLimitTests` |
| E2 | ORCID-based author disambiguation | **Met (5.1), with stated limits** | ORCID iDs from operator input (validated by ISO 7064 checksum) and from manuscript front matter; reference ORCIDs from OpenAlex and Crossref. Two different iDs are two people regardless of name. **Limits:** iDs are attached to named authors only when the count matches; many index records lack ORCIDs, in which case the name-and-initial tier applies. The ORCID registry itself is not queried. | `SelfCitationTests` |
| E3 | Retrieval-augmented verification of suggested citations | **Met (5.1)** | Each model lead is resolved with the bibliography's own identity rules: *verified*, *ambiguous match*, *no matching record*, or *verification unavailable*. Unmatched leads raise an informational item warning not to cite them. | `LiteratureGapTests.test_model_leads_are_verified_or_marked_as_unmatched` |
| E4 | Broader metadata and publisher coverage | **Met (5.2)** | Primary: OpenAlex, Crossref, Semantic Scholar; full text via Unpaywall, arXiv and PMC. Supplementary, queried only for references the primary indexes cannot resolve: **dblp** (computer science venues), **Europe PMC** (life sciences, with abstracts, ORCIDs and retraction status), **DataCite** (datasets, software, arXiv and Zenodo DOIs; also consulted for DOI registration). Crossref retractions via `updated-by`. | `EnrichmentTests`, `test_coverage_providers` |
| E5 | Stronger offline deployment options | **Met (5.1), with stated limits** | `CITEPRISM_OFFLINE_MODE`; `openai_compatible` provider for extraction, assessment and leads (Ollama, vLLM, LM Studio); local or lexical similarity; `compose.offline.yml`; configuration validation rejects any network-dependent setting in offline mode. **Limit:** no offline copy of a scholarly index, so references cannot be resolved offline; they are reported as *not resolved (offline mode)*, never as missing. | `OfflineConfigurationTests`, `EnrichmentTests.test_offline_mode_…` |
| E6 | Evaluation on larger multi-domain annotated datasets | **Partly met (5.4): synthetic benchmark; annotated data still missing** | **5.4:** `scripts/benchmark_injected.py` / `src/evaluation/injection.py` — an error-injection benchmark with ground truth by construction (citation year typos, dropped citations, metadata perturbations, identity decoys, author-name forms). On the 263-page German volume: 100% found, no false alarms (docs/BENCHMARK.md); gated in CI on the bundled volume. It measures the rule-based detectors only. Earlier infrastructure: 5.2 adds cross-validated threshold selection (tune on k−1 manuscript folds, report on the held-out fold). Labels for every objective (identity, support incl. `unrelated`, relevance, self-citation, metadata, extraction, triage); domain field and per-domain breakdown; manuscript-level bootstrap confidence intervals; AUROC and F1-optimal threshold selection for relevance; calibration (Brier, ECE, reliability table); blind annotation templates (`scripts/make_annotation_template.py`); Markdown + LaTeX report (`--markdown`). **Not met:** no large annotated dataset exists. It needs human annotators and cannot be generated. `data/evaluation/example_gold_set.jsonl` illustrates the format only. | `EvaluationTests` |

### Expected outcome and publication goal

| ID | Goal | Status | Notes |
|---|---|---|---|
| G1 | Practical research-integrity tool | Met | Deployable runtime, review workflow, signed exports |
| G2 | Refine, validate and position for publication | Infrastructure only | Reproducibility is in place: every artifact records method versions, configuration fingerprints, model identifiers, prompt versions and exact provider calls. Validation requires the dataset from E6. A defensible paper needs at least two annotators (κ is reported), several domains, and thresholds tuned on one split and reported on another. |

## What a reviewer now sees

New review items, all phrased as prompts for human judgement:

| Category | Priority | Raised when |
|---|---|---|
| `potentially_irrelevant` | moderate | Grounded hybrid relevance is below the borderline threshold, or any citation use was judged *unrelated* |
| `low_topical_similarity` | informational | Topic similarity is low and no grounded assessment exists |
| `questionable_self_citation` | moderate | A self-citation is never cited in the text, or may be irrelevant |
| `self_citation_pattern` | moderate | Rate, author-concentration, padding or low-relevance patterns cross their thresholds |
| `metadata_difference` | high / moderate / low | Unregistered DOI on an unfound work / major difference / minor difference |
| `possibly_missing_literature` | low | A work is cited by three or more of the manuscript's own references |
| `citation_year_mismatch` (5.3) | moderate | The text cites a work with a year the reference list does not give |
| `multiple_reference_lists` (5.3) | moderate | The PDF has several reference lists (book, proceedings); recommends checking chapter by chapter |
| `extraction_reliability` (5.3) | high | The document could not be read well enough for per-reference results to be trusted |
| `read_without_model` (5.3) | informational | The reference list was read by rule because no model was available |
| `unverified_search_leads` | informational | A model lead matches no real record |

## Verification status of this release

| Check | Where | Result |
|---|---|---|
| Unit and acceptance tests (no HTTP, no PostgreSQL) | Claude's sandbox, real pydantic 2.13 | 558 tests across 28 modules: 534 pass, 24 skip (PostgreSQL and live-provider tests), 0 fail. The HTTP (FastAPI) and Streamlit suites need packages not installable there and are covered by CI and `verify_phase4.ps1` |
| Web interface | Claude's sandbox: esbuild bundle against React 19, rendered in Chromium against API responses generated by the Python code | Every screen renders without console errors in light and dark schemes and at 390 px width with no horizontal scroll. Type-checked against minimal React stubs; the full `tsc` build runs in CI |
| HTTP API tests (`test_phase3_api`, `test_phase4_api`) | GitHub Actions / local `verify_phase4.ps1` | Must pass before merge |
| React `npm run typecheck` and `build` | GitHub Actions / local | Must pass before merge |
| PostgreSQL workspace tests | `verify_phase4.ps1 -WithPostgres` | Must pass before a release |
| Secret scan | CI job `secrets`, `verify_phase4.ps1` | Must pass on every push |
