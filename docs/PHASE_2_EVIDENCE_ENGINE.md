# Phase 2 — citation evidence engine

Release: `3.1.0a2`. This release builds on Phase 1, including the Windows object-store fix. It prepares the evidence contracts for the React/TypeScript and FastAPI workspace in Phase 3.

## What changed

| Area | Behavior in this release |
|---|---|
| Extraction | Detects numbered reference headings, preserves every printed numeric reference slot, and anchors bibliography entries and citation occurrences to PDF page text. |
| Citation identity | Uses PDF hash and occurrence offsets. Repeated text at different locations remains separate. |
| Bibliography ambiguity | Keeps incomplete entries and flags possible split entries. It does not silently merge, drop or renumber them. |
| Metadata resolution | Compares titles, author surnames and identifiers, retains candidates and conflicts, and abstains on ties or insufficient identity evidence. |
| Source retrieval | Records OpenAlex abstract snapshots. A Crossref fallback must use the resolved DOI and return that DOI. Unverified title-search abstract substitution is removed. |
| Assessment | Uses opaque occurrence IDs. The model receives the target citation marker and offsets, its manuscript context, and only its assigned source abstract. |
| Grounding | A claim quotation must cover the target citation marker. Every accepted source quotation must occur in that reference's own snapshot. Only whitespace normalization is permitted. |
| States | Unresolved identity, uncited entries, absent source text, provider failure, grounding failure, and grounded model proposals remain distinct. |
| Scoring | Topic similarity is optional and off by default. It does not become a citation-support verdict. Legacy hybrid score fields remain null in new results. |
| Providers | Uses the maintained Google Gen AI client, bounded retries, explicit timeouts, safe error categories, and individually recorded request/response artifacts. |
| Cache | Includes method/configuration versions. Re-extraction refreshes downstream stages. Source lookups expire after seven days; a manual refresh is available. |
| Provenance | Retains PDF text, source snapshots, model-call identities, source locations and proposals in the result. The trust adapter captures exact result bytes and appends the corresponding records. |
| Review UI | Four destinations: New audit, Review, Evidence and Diagnostics. Review counts and exports come from one versioned projection. |
| Export | Portable UTF-8 HTML report, review JSON, and complete evidence JSON. Untrusted strings are escaped in HTML. |

## Supplied manuscript regression case

The included fixture is the six-page **Underwater Communication System for Scuba Divers** manuscript supplied in this collaboration. The earlier result is retained solely as a regression input.

The deterministic checks establish:

- Six located citation occurrences: `[1]`, `[9]`, `[4]`, `[3]`, `[5]`, `[2]`, all on page 2.
- Ten printed bibliography slots are preserved.
- Slots 8 and 9 receive a possible-split-entry flag. This is not an automatic declaration that the paper contains ten distinct works.
- Previously selected candidates for references 1 and 10 fail the author-identity check.
- A strong title-and-author match may retain a publication-year discrepancy for review rather than erasing it.
- Quotations from an unrelated source are rejected.

The offline replay uses the earlier metadata responses and simulated model proposals. It does **not** represent a new live-provider audit or measure scientific accuracy. A more conservative result can contain more unresolved items than the earlier run; it exposes the available evidence rather than manufacturing a clean score.

## Review states

| State | Meaning | Next action |
|---|---|---|
| `identity_unresolved` | A source has not been established for the printed reference. | Compare bibliography text with candidate identity signals. |
| `not_cited` | Supported patterns did not locate an in-text use. | Check the manuscript or citation format. |
| `source_unavailable` | A resolved work has no usable abstract available to this run. | Obtain source text before assessing support. |
| `assessment_unavailable` | A provider failed, omitted an item, duplicated an ID or returned an invalid schema. | Inspect the diagnostic code and retry assessment. |
| `grounding_failed` | A quotation could not be linked to the assigned source or target citation. | Inspect the rejected model observation; no support verdict is accepted. |
| `grounded_proposal` | Required quote locations were validated. | Review the interpretation; quote attribution does not establish scientific correctness. |

A model proposal is one of `supports`, `contradicts`, `context_only`, or `insufficient_evidence`. These are unreviewed proposals. A reference with several occurrences can have mixed proposals. The product does not label missing evidence “irrelevant” or “clean.”

“DOI omitted in manuscript” and “no DOI returned for a resolved work” are separate informational findings.

## PostgreSQL migration

`002_extraction_observations.sql` scopes reference ordinals by the extraction revision in existing bibliographic metadata. This lets an amended parse coexist with its predecessor for the same PDF version.

Existing record bodies, record hashes, audit events and Phase 1 migration bytes are unchanged. The original reference-key constraint remains. Tenant isolation, append-only triggers and foreign keys remain enforced. The in-memory adapter mirrors the revised ordinal constraint.

Apply migrations through the existing admin migration command. The Windows verification script starts the existing disposable development PostgreSQL service, applies migrations, and requires the live tests when `-WithPostgres` is supplied. Production migration planning remains part of Phase 5.

## Install and run on Keyur's Windows machine

1. Stop the current Streamlit terminal with **Ctrl+C**.
2. Extract `CITEPRISM_PHASE2_RELEASE.zip` to `C:\Users\keyur\Downloads\CitePrism_Phase2`.
3. In the VS Code PowerShell terminal, run this block:

```powershell
& {
    $ErrorActionPreference = "Stop"
    Set-Location "C:\Users\keyur\Citeprism"
    $phase2Python = ".\.venv-phase1\Scripts\python.exe"
    & $phase2Python "C:\Users\keyur\Downloads\CitePrism_Phase2\APPLY_PHASE2.py" --project-root . --apply
    if ($LASTEXITCODE -ne 0) { throw "Phase 2 files were not installed. Read the message above." }
    & $phase2Python -m pip install -r requirements-phase2.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
    & .\scripts\verify_phase2.ps1 -Python $phase2Python -WithPostgres
}
```

The installer validates all expected source files before changing anything, accepts equivalent Windows/Linux line endings, backs up replaced files, and creates a local Phase 2 branch. It leaves credentials, data and unrelated edits in place. It does not commit or push. If a source file has unexpected edits, it stops and names that file instead of guessing how to merge it.

4. After verification passes, run:

```powershell
& .\scripts\start_phase2.ps1 -Python .\.venv-phase1\Scripts\python.exe
```

Open `http://localhost:8502`. Choose **Open workspace → New audit**, upload the manuscript and select **Run citation audit**. Use **Review** for the queue, **Evidence** for the manuscript/source comparison, and **Diagnostics** for failed provider calls or full artifacts.

For subsequent launches, use the same `start_phase2.ps1` command. No environment activation is required.

If Windows prevents a downloaded script from running, first inspect it, then use `Unblock-File` on that specific script. Do not change the machine-wide execution policy.

## Configuration

Keep the working `.env` from your successful run. The extraction model remains the value configured there; this release does not invent or silently substitute a model ID.

- `LLM_PROVIDER`, `GOOGLE_API_KEY` / `OPENAI_API_KEY`, and the corresponding model select extraction.
- `HF_API_TOKEN` and `HF_MODEL` select claim assessment. These are separate from extraction credentials.
- `OPENALEX_API_KEY` supplies your OpenAlex account key to metadata requests. It is not included in recorded query artifacts.
- `ENABLE_TOPIC_SIMILARITY=false` is the default. Set it to true only after installing the optional embedding dependencies from `requirements.in`.
- `HF_BATCH_SIZE` is capped at five occurrence assessments. Extraction output tokens are bounded at 32,000.

Current Google SDK usage is described in the [official migration guide](https://ai.google.dev/gemini-api/docs/migrate). OpenAlex documents account/API access through its [official help center](https://help.openalex.org/).

## Verification

```powershell
& .\.venv-phase1\Scripts\python.exe .\scripts\replay_phase2.py
```

This generates an explicitly labelled offline replay report under `.trust-verify\phase2-replay`. It uses no API keys and makes no provider calls.

`verify_phase2.py` writes machine-readable passed, failed and skipped counts. PostgreSQL tests never silently become in-memory tests. CI runs the deterministic suite on Windows/Linux and Python 3.11–3.13, plus a PostgreSQL 16 job and an installed-wheel check.

The delivered verification record states exactly which checks ran in the build workspace. A passing offline gate is not evidence that the live provider credentials, quotas or PostgreSQL installation on your laptop are working.

## Known boundaries and next phase

- Text-based PDFs and numeric bibliographies have the strongest coverage. Unlocated/unnumbered entries remain visible for review. Scanned PDFs need an OCR phase; visual PDF bounding boxes are not generated here.
- Source support is assessed from retrieved abstracts. Full-text acquisition, licensing-aware retrieval and independent benchmark labels remain outstanding.
- Author identity checks use surname overlap and title similarity. They are conservative rules, not calibrated identity probabilities.
- The reviewer workspace is local Streamlit. User accounts, durable reviewer decisions, API boundaries and the React/TypeScript workspace are Phase 3.
- Multi-user concurrency, durable background jobs, operational deployment, load testing and release evaluation remain in Phases 4–6. This release is not a certification of governmental procurement readiness.

The Phase 3 starting point is `ReviewProjection`, the source/occurrence contracts and the Phase 1 trust repository. The same review policy can then serve a typed API and React client without rebuilding the evidence logic again.
