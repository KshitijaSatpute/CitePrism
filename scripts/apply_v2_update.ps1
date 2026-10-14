<#
Applies the CitePrism V2.0 update overlay to an existing CitePrism checkout.

    cd <repo>
    .\scripts\apply_v2_update.ps1 -Overlay C:\Downloads\CITEPRISM_V2_OVERLAY.zip

What it does:
  1. Refuses to run on a dirty working tree (commit or stash first).
  2. Creates branch "v2-update" from the current branch.
  3. Copies the overlay's files over the repository (never touches .env, data, secrets keys).
  4. Stops tracking secrets/ and .env if they are tracked (local copies are kept).
  5. Runs the secret check and the V2.0 acceptance tests.
It does not commit or push; review `git diff --stat` and commit yourself.
#>
param(
    [Parameter(Mandatory = $true)][string]$Overlay,
    [string]$Branch = "v2-update"
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path $Overlay)) { throw "Overlay not found: $Overlay" }
$Overlay = (Resolve-Path $Overlay).Path
$ErrorActionPreference = "Continue"
$root = git rev-parse --show-toplevel 2>$null
$ErrorActionPreference = "Stop"
if (-not $root) { throw "Run this from inside your CitePrism git folder (cd into it first)." }
$root = $root.Trim()
Set-Location $root

if (git status --porcelain) { throw "Working tree has uncommitted changes. Commit or stash them first." }
git switch -c $Branch
if ($LASTEXITCODE -ne 0) { throw "Could not create branch $Branch (does it already exist?)." }

$staging = Join-Path ([System.IO.Path]::GetTempPath()) ("citeprism-v2-" + [guid]::NewGuid())
Expand-Archive -Path $Overlay -DestinationPath $staging
$source = Get-ChildItem $staging | Where-Object { $_.PSIsContainer } | Select-Object -First 1
if (-not $source -or -not (Test-Path (Join-Path $source.FullName "src"))) { $source = Get-Item $staging }

$protected = @(".env", "data", ".venv-phase4", "node_modules")
Get-ChildItem -Path $source.FullName -Recurse -File | ForEach-Object {
    $relative = $_.FullName.Substring($source.FullName.Length + 1)
    $top = $relative.Split([IO.Path]::DirectorySeparatorChar)[0]
    if ($protected -contains $top) { return }
    if ($relative -like "secrets*" -and $_.Name -ne "README.md") { return }
    if ($relative -eq "V2_CHANGED_FILES.txt") { return }
    $target = Join-Path $root $relative
    New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
    Copy-Item $_.FullName $target -Force
}
Remove-Item $staging -Recurse -Force

# Stop tracking secrets. A signing key that was committed is compromised: delete
# it so setup_phase4.ps1 -GenerateSigningKey creates a fresh one (it never
# overwrites an existing key). .env is only untracked; your local copy stays.
$leakedKeys = @(git ls-files -- secrets | Where-Object { $_ -ne "secrets/README.md" })
foreach ($key in $leakedKeys) {
    git rm --cached --quiet -- $key
    if (Test-Path $key) { Remove-Item $key -Force }
    Write-Host "Removed committed key $key - it was public; a new one will be generated."
}
if (git ls-files -- .env) { git rm --cached --quiet -- .env; Write-Host "Untracked .env (local copy kept). Rotate its API keys." }
if (Test-Path secrets/README.md) { git add secrets/README.md }

$venvPython = ".venv-phase4\Scripts\python.exe"
if (Test-Path $venvPython) {
    & $venvPython scripts/check_no_secrets.py
    & $venvPython -m unittest tests.test_v2_requirements tests.test_assistant tests.test_provider_fallback tests.test_coverage_providers tests.test_author_year tests.test_book_references tests.test_chat_breadth tests.test_injection_benchmark tests.test_data_policy tests.test_conversation tests.test_hardening tests.test_review_fixes
    if ($LASTEXITCODE -ne 0) { throw "V2.0 acceptance tests failed." }
} else {
    & py -3.12 scripts/check_no_secrets.py
    Write-Host "No .venv-phase4 yet: run .\scripts\setup_phase4.ps1 -GenerateSigningKey -WithEmbeddings, then .\scripts\verify_phase4.ps1."
}

Write-Host ""
Write-Host "Overlay applied on branch $Branch. Next:"
Write-Host "  git add -A ; git status            # review"
Write-Host "  .\scripts\verify_phase4.ps1 -Python .\.venv-phase4\Scripts\python.exe   # full suite"
Write-Host "  git commit -m 'CitePrism V2.0 update' ; git push -u origin $Branch   # then open a pull request"
Write-Host "If a key was ever pushed, rotate it: docs\SECURITY_KEY_ROTATION.md"
