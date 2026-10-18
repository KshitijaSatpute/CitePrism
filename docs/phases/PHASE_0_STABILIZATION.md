# CitePrism Phase 0 — Stabilization Slice

## Outcome

This patch turns the supplied V2 review bundle into a safer and more honest
demonstration baseline. It does **not** make CitePrism production-ready or
federal-ready. The next architectural phase remains the V3 trust kernel.

## Acceptance gates completed

- All Python files compile.
- Sixteen deterministic regression tests pass without network access or API keys.
- Configuration can be imported without provider credentials or `python-dotenv`.
- Provider credentials are rejected when an LLM-backed feature actually starts.
- Two PDFs with the same display filename cannot overwrite one another.
- The same PDF content deduplicates to one canonical stored artifact.
- JSON and PDF copies use atomic replacement.
- A model/provider failure is represented as `unavailable`, never score `0` or `50`.
- An incomplete scoring run cannot receive a Low/Medium/High summary.
- Existing quality flags and self-citation evidence survive later stages.
- Metadata resolution ranks up to five candidates and abstains below confidence.
- Evaluation uses a pre-recorded threshold instead of tuning on gold labels.

## Material behavior changes

### Ingestion and storage

New uploads are staged only after **Start Citation Audit** is pressed. The
orchestrator checks the file signature and size, hashes it, and stores it as
`data/raw_pdfs/<sha256>.pdf`. The original filename is stored separately for UI
display. An automatic SQLite migration adds `original_filename` to existing
databases.

SQLite now enables foreign keys, a 30-second busy timeout, and WAL mode. Record
deletion removes only artifacts resolved beneath this project's `data`
directory; an unexpected external path is refused.

### Artifacts and pipeline contracts

Stage results accumulate rather than overwriting previous `stages_completed`,
`file_paths`, and errors. Parsed, enriched, scored, risk, and suggestion JSON
updates are written atomically. Parsing cache identity includes PDF hash,
parser version, provider, and model.

### Extraction

- Long single paragraphs now obey the chunk-size ceiling.
- Manuscript DOI extraction stops before the bibliography and is limited to
  front matter.
- Regex and LLM citation results are merged instead of selecting whichever
  list is longer.
- Obvious year/observation false positives are rejected.
- Partial chunk failures are persisted in `extraction_metadata` with coverage
  counts and warnings.
- Prompts explicitly treat manuscript content as untrusted data.

### Enrichment and scoring

- OpenAlex candidates are ranked; DOI equality wins, otherwise a minimum title
  similarity is required. Low-confidence results abstain.
- Abstract provenance is recorded as OpenAlex, Semantic Scholar, Crossref,
  arXiv, or publisher HTML.
- Publisher-page scraping is disabled by default.
- Missing/failed embedding or LLM values yield an unavailable final score.
- Score artifacts now record model names, weights, thresholds, method/prompt
  versions, UTC generation time, and unavailable count.
- LLM evidence is explicitly marked `model_generated_unverified`.

### UI, reports, and evaluation

- Unavailable results are separate from below-threshold and above-threshold
  results.
- Exported and inline HTML escapes manuscript/model-controlled text.
- Large PDF inline previews are disabled above 10 MiB while download remains.
- PyVis assets are inlined and temporary graph files are deleted.
- Age and author overlap are presented descriptively, without inferring bias,
  padding, poor quality, or misconduct.
- Literature suggestions are labeled unverified search candidates.
- Gold-label evaluation reads the threshold from scoring metadata and does not
  expose a test-set tuning slider.

## Apply to Janvi's canonical branch

Start from a clean branch created from the commit that produced the supplied
review bundle. Do not apply directly to `main`.

```powershell
git switch -c fix/phase0-stabilization
git apply --check .\CITEPRISM_PHASE0.patch
git apply --index .\CITEPRISM_PHASE0.patch
python -m compileall -q .
python -m unittest discover -v
git diff --cached --check
git commit -m "fix: stabilize CitePrism V2 evidence pipeline"
```

If `git apply --check` reports conflicts, stop and use the complete changed-file
bundle as a reference rather than overwriting teammate work. The supplied ZIP
did not contain canonical Git history, so the patch is the authoritative
handoff format for this slice.

## Verification performed here

```text
python -m compileall -q .                         PASS
python -m unittest discover -v                    PASS (16 tests)
configuration import without API keys             PASS
feature-time missing-key validation                PASS
```

No live Gemini, OpenAI, Hugging Face, OpenAlex, publisher, end-to-end Streamlit,
or corpus evaluation was run because the review bundle intentionally excluded
credentials and research data. Those are explicit integration gates on Janvi's
machine after patch application.

## Remaining release blockers

- The parser still lacks page/bounding-box provenance and robust OCR/layout recovery.
- Citation relevance is still an abstract/manuscript similarity proxy, not
  claim-to-source support verification.
- SQLite and mutable local artifacts remain a single-node prototype boundary.
- There is no authentication, authorization, tenant isolation, workflow,
  immutable audit ledger, retention policy, or legal hold.
- The risk weights are uncalibrated and restricted to prototype triage.
- Dependencies need platform-specific hashed lock files plus SBOM and license review.
- A multi-document, multi-domain, adjudicated holdout evaluation is still required.
- University/IP ownership, dataset rights, and an open/commercial license must be resolved.

## Next slice

Phase 1 should build the V3 trust kernel: typed immutable entities for document
versions, source spans, references, candidate matches, evidence, model runs,
findings, reviewer decisions, and audit events backed by PostgreSQL and a
versioned object store. The V2 Streamlit app should remain only a demonstration
shell during that work.
