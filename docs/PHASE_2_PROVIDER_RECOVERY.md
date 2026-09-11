# Phase 2 hotfix: recover from temporary provider failures

The reported run extracted 22,283 PDF characters. Its manuscript metadata call
succeeded after two HTTP 503 responses. The following bibliography request
received three HTTP 503 responses, so extraction correctly stopped incomplete.
The successful HTTP 200 shows that the configured model and credentials worked
for that request. It does not establish that subsequent requests will succeed.

Google documents 503 UNAVAILABLE as a temporary availability/capacity problem:
https://ai.google.dev/gemini-api/docs/troubleshooting

## What changes

- Identify HTTP 503 as `service_unavailable`, retain its status and attempt count,
  and display an actionable message. Authentication/configuration failures stay
  distinct and are not retried.
- Keep at most three attempts per request and the existing 120-second SDK
  timeout. Automatic waits use exponential backoff (5–6 seconds, then 10–11
  seconds by default). A valid Retry-After can extend a wait up to 30 seconds;
  a longer provider-requested delay stops the automatic loop for a later manual
  retry. Attempts are saved before sleeping. Delays do not guarantee availability.
- Explicitly disable unused Google SDK automatic function calling. The AFC
  warning was incidental to the outage, not its cause.
- Save validated metadata and individual completed bibliography chunks
  atomically under `data/debug/extractor_responses/*.checkpoint.json`.
- Resume only when PDF hash, extracted text, prompts, configured provider/model,
  and output-token setting match. Preserve original successful and failed call
  records; no reused response is recorded as a new provider execution.
- An interrupted parse remains incomplete. No replacement audit, fabricated
  bibliography, or score is created to hide a failed request.
- Clear the partial checkpoint after successful PDF anchoring. Completed-stage
  caches continue working as before. An explicit new extraction after success
  makes fresh model requests.
- Show retry instructions in the upload view. Expected provider failures no
  longer fill the UI with Python tracebacks; internal errors retain tracebacks.

Checkpoint files contain manuscript responses and share the existing local
debug directory. Their checksum detects accidental corruption; it is not a
cryptographic signature. Checkpoints support recovery of requests made after
this hotfix. The previously reported run did not create one.

## Install on Windows

1. Stop Streamlit with Ctrl+C in its terminal.
2. Download CITEPRISM_PHASE2_PROVIDER_RECOVERY.zip to Downloads.
3. Paste the following in PowerShell:

```powershell
& {
    $ErrorActionPreference = "Stop"
    Set-Location "C:\Users\keyur\Citeprism"
    $recoveryFolder = "C:\Users\keyur\Downloads\CitePrism_Provider_Recovery"
    Expand-Archive -LiteralPath "C:\Users\keyur\Downloads\CITEPRISM_PHASE2_PROVIDER_RECOVERY.zip" -DestinationPath $recoveryFolder -Force
    $recoveryPython = ".\.venv-phase1\Scripts\python.exe"
    & $recoveryPython "$recoveryFolder\APPLY_PROVIDER_RECOVERY.py" --project-root . --apply
    if ($LASTEXITCODE -ne 0) { throw "Installer stopped. Read the message above." }
    & .\scripts\verify_phase2.ps1 -Python $recoveryPython -WithPostgres
}
```

Keep Docker Desktop running for the verification step. No new dependencies or
database migrations are introduced. The installer checks all changed source
files before editing, accepts Windows line endings, creates backups, and does
not commit or push. A mismatch stops installation without overwriting edits.

4. After verification passes, launch:

```powershell
& .\scripts\start_phase2.ps1 -Python .\.venv-phase1\Scripts\python.exe
```

5. Open http://127.0.0.1:8502, choose New audit, upload the same PDF, and click
   Run citation audit with advanced options unchecked. If Google still returns
   503, wait a few minutes before retrying. Successful steps from the updated
   run will be reused; retry logs identify the reused steps.

## Verification limits

The hotfix includes regression cases for the exact reported partial failure,
multiple bibliography chunks, model/PDF changes, malformed responses,
checkpoint corruption, authentication failures, retry budgets, Google SDK
configuration, and the user-facing error state.

This session ran nine core recovery tests successfully and compiled all changed
Python files. Two integration tests (Google SDK and Streamlit AppTest) were
skipped because those dependencies were unavailable in the refreshed execution
environment. Dependency installation could not complete. The full Phase 2
suite, live PostgreSQL, and fresh credentialed model requests were not rerun
here. Run the supplied verification command in the existing laptop environment
before accepting the hotfix. Prior release test totals are not presented as
validation of this change.

Google SDK automatic function calling configuration:
https://googleapis.github.io/python-genai/#disabling-automatic-function-calling
