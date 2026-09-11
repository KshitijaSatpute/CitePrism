# Phase 2: assessment response compatibility and diagnostics

## What the uploaded run establishes

The supplied evidence artifact from September 9 contains ten linked bibliography
entries, six citation occurrences, four resolved sources, and three assessment
tasks. Every recorded provider call succeeded. The Hugging Face response is a
complete JSON list containing answers for all three exact task IDs.

The application expected an object with an `assessments` member. It rejected
the bare list and recorded `invalid_response` for references 3, 4 and 5. The
provider table correctly reported API success, but the UI did not clearly
explain that response validation had failed afterwards.

There was also a PDF layout mismatch in reference 4's claim: the PDF text
contains `real-` followed by a newline and `time`, while the model returned
`real-time`. Its original characters and citation location remain available.

## Changes

- Accept both a complete JSON list and the named `assessments` envelope.
  Truncated JSON, prose after JSON, non-object rows and invalid envelopes still
  fail. Task identity, duplicate detection, row schemas and quotations are
  independently checked before any proposal is accepted.
- Locate manuscript claims across a line break immediately after a retained
  word hyphen. Do not change wording, remove the hyphen, or apply this rule to
  source abstracts. Store the original manuscript slice and character offsets.
- Record per-task failures in scoring diagnostics, with reference and call IDs.
- Separate API call status from assessment acceptance in the Diagnostics view.
  Show citation tasks submitted, passed quotation checks and tasks needing
  attention. Explain response-format and quotation errors in plain language.
- Associate every exported review finding with its reference number and title.
- Upgrade the scorer to `grounded-assessment-v3.1`, so the next audit refreshes
  assessment while valid extraction and metadata caches remain reusable.
- Retain the recorded assessment version during trust capture; replaying an
  earlier call must not rewrite its immutable method metadata.
- Include the previously supplied Streamlit preview-test compatibility fix.

## Replay of the recorded response

| Reference | Identity | Replay outcome |
| --- | --- | --- |
| 1, 2, 10 | Unresolved | Returned candidates remain unaccepted; title/author evidence conflicts |
| 3 | Resolved | Located proposal: insufficient evidence |
| 4 | Resolved | Located proposal: context only |
| 5 | Resolved | Located proposal: insufficient evidence |
| 6 | No match found | No assessment |
| 7 | Resolved | No in-text occurrence located; no assessment |
| 8, 9 | Bibliography needs review | Possible split entry; numbering preserved |

All three recorded proposals pass attribution checks after this change.
None proposes verified support for the manuscript's claims. No new provider
requests were made to produce this replay, and no human decision was recorded.

Interpretation quality remains a separate concern. For example, reference 3's
abstract explicitly discusses 5 Mbps and error correction, while the model's
rationale broadly says the abstract only mentions underwater sensors and
optical communication. The abstract does not establish every part of the
manuscript's synchronization claim, but the rationale still overlooks relevant
evidence. Future work must improve claim decomposition, evidence selection and
evaluation using independently reviewed examples.

## Windows steps

1. Save `CITEPRISM_PHASE2_ASSESSMENT_FIX.zip` in Downloads.
2. Stop CitePrism with Ctrl+C. Keep Docker Desktop running.
3. Paste this whole block into the VS Code PowerShell terminal:

```powershell
& {
    $ErrorActionPreference = "Stop"
    Set-Location "C:\Users\keyur\Citeprism"
    $python = ".\.venv-phase1\Scripts\python.exe"
    $assessmentFolder = "C:\Users\keyur\Downloads\CitePrism_Assessment_Fix"
    Expand-Archive -LiteralPath "C:\Users\keyur\Downloads\CITEPRISM_PHASE2_ASSESSMENT_FIX.zip" -DestinationPath $assessmentFolder -Force
    & $python "$assessmentFolder\APPLY_ASSESSMENT_FIX.py" --project-root . --apply
    if ($LASTEXITCODE -ne 0) { throw "Installer stopped. Read the message above." }
    & .\scripts\verify_phase2.ps1 -Python $python -WithPostgres
}
```

The installer checks the expected source files and backs up replacements. It
accepts the previous preview test either before or after its compatibility fix.
It creates a local fix branch and performs no commit or push. There are no new
runtime dependencies, database migrations or credential changes in this patch.

4. After verification passes, restart:

```powershell
& .\scripts\start_phase2.ps1 -Python .\.venv-phase1\Scripts\python.exe
```

5. Choose New audit, upload the same PDF, leave advanced options unchecked and
   run the audit. A changed scorer version triggers assessment automatically.
   Opening the previous saved result alone will not replace its old assessments.
   Fresh model answers may differ from the recorded replay.
6. Inspect Review, Evidence and Diagnostics. If an answer fails validation,
   Diagnostics now identifies its reference and failure reason separately from
   the API call status.

## Validation and next product work

The package includes a recorded-response fixture, replay summary, review HTML,
and machine-readable regression report. Tests cover list/envelope handling,
exact task association, invalid responses, foreign quotations, target citation
anchors, Windows/PDF line breaks, immutable trust capture, and UI diagnostics.
Inspect the verification report for explicit PostgreSQL skips in this environment;
the Windows command above requires those checks against your Docker database.

The next major product phase is the React/TypeScript workspace and API: a review
queue with manuscript/source views, saved reviewer decisions and bibliography
corrections, clear job states and recoverable processing. Model-quality work
must continue alongside that workflow; a new frontend does not validate the
scientific interpretation.
