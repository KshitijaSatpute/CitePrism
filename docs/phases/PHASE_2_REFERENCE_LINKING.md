# Phase 2 hotfix: numbered reference linking and PDF preview

## Confirmed cause

The uploaded CitePrism_evidence_artifact.json contains two successful Gemini
responses, ten parsed references labelled `1)` through `10)`, and no external
source lookup calls. The old matcher removed square brackets and whitespace
but retained the closing parenthesis. Its model key `1)` did not match the
printed-slot key `1`. All ten metadata records became unassigned, so source
lookup had no titles or identifiers and claim assessment received zero tasks.

This was an application matching defect. The earlier regression fixtures used
bracketed identifiers and missed the actual model's perfectly valid label
format. The new regression fixture preserves the supplied responses verbatim.

## Changes

- Normalize standalone numeric labels consistently: `1`, `[1]`, `1)`, `1.`,
  `(1)`, and leading-zero equivalents. Do not reduce author/year identifiers,
  alphanumeric labels, ranges, or arbitrary text to a number.
- Preserve the printed label/text, page offsets and original model label.
  Duplicate model observations and duplicate printed labels still need review.
- Keep title, author and year grounding checks and conservative external source
  identity checks. Matching a reference number does not verify its publication.
- Record linked, unassigned and review-needed entry counts in extraction
  metadata. Update extraction method to `page-anchors-v3.1`; the next audit
  invalidates the earlier parsed result and refreshes its downstream stages.
- Explain failed reference linking as an application/extraction issue, rather
  than implying that every printed entry is incomplete. Translate technical
  issue codes into readable messages in the review and HTML export.
- Replace the nested PDF iframe with a rendered PNG page, a page selector and
  the original PDF download. The preview opens at the reference's bibliography
  page. It displays the original page without claiming citation highlighting.
- Add `pypdfium2>=5.3,<6`. Serialize PDFium access across Streamlit threads and
  cache only a bounded number of page previews. Confirm the displayed PDF hash
  matches the audited document when the artifact supplies one.

## Replay result

Using the actual recorded responses and the same six-page PDF:

| Check | Previous result | Corrected replay |
| --- | ---: | ---: |
| Printed reference entries retained | 10 | 10 |
| Located citation occurrences | 6 | 6 |
| Linked model reference records | 0 | 10 |
| Unassigned model reference records | 10 | 0 |
| Entries with grounded metadata and unambiguous boundaries | 0 | 8 |
| Entries needing extraction review | 10 | 2 |

Entries 8 and 9 remain for manual inspection because the printed bibliography
appears to split one work across two numbers. The corrected replay does not
claim eight resolved external sources. A fresh source lookup is still needed.
Previously supplied wrong-author candidates for references 1 and 10 remain
rejected. No new external provider calls were made during these checks.

## Windows installation

1. Stop the running app with Ctrl+C and keep Docker Desktop running.
2. Download CITEPRISM_PHASE2_REFERENCE_LINKING.zip into Downloads.
3. Run this whole block in the VS Code PowerShell terminal:

```powershell
& {
    $ErrorActionPreference = "Stop"
    Set-Location "C:\Users\<you>\Citeprism"
    $linkingFolder = "C:\Users\<you>\Downloads\CitePrism_Reference_Linking"
    Expand-Archive -LiteralPath "C:\Users\<you>\Downloads\CITEPRISM_PHASE2_REFERENCE_LINKING.zip" -DestinationPath $linkingFolder -Force
    $linkingPython = ".\.venv-phase1\Scripts\python.exe"
    & $linkingPython "$linkingFolder\APPLY_REFERENCE_LINKING.py" --project-root . --apply
    if ($LASTEXITCODE -ne 0) { throw "Installer stopped. Read the message above." }
    & $linkingPython -m pip install -r requirements-phase2.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
    & .\scripts\verify_phase2.ps1 -Python $linkingPython -WithPostgres
}
```

The installer checks the previous source files, accepts Windows line endings,
backs up replacements, and stops on unexpected edits. It does not commit or
push. No database migration or API-key change is included.

4. After verification passes, restart:

```powershell
& .\scripts\start_phase2.ps1 -Python .\.venv-phase1\Scripts\python.exe
```

5. Open http://127.0.0.1:8502 and choose New audit. Upload the same PDF and click
   Run citation audit. Leave the advanced options unchecked. Opening the old
   saved result alone will not regenerate its missing source metadata.
6. Inspect Review and Evidence. Titles and authors should now populate for the
   complete entries. Resolved-source and assessment counts depend on the live
   metadata/model responses and may include legitimate unresolved items.

## Verification

22 focused tests passed here, including the actual response replay, numeric
format variants, duplicate labels, preserved grounding, wrong-author rejection,
PDF rendering/page bounds/concurrent access, and previous provider recovery.
Three SDK/UI integration tests were skipped because Google SDK and Streamlit
were not installed in this execution environment. The full suite, live
PostgreSQL, live source lookup and fresh LLM audit remain laptop checks. The
rendered bibliography page was visually inspected successfully.

PDF renderer API and threading requirements:
https://pypdfium2.readthedocs.io/en/stable/python_api.html
