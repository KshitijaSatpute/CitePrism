[CmdletBinding()]
param(
    [string]$Python = ".\.venv-phase3\Scripts\python.exe",
    [switch]$WithPostgres
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    & "$PSScriptRoot\verify_phase2.ps1" -Python $Python -WithPostgres:$WithPostgres
    if ($LASTEXITCODE -ne 0) { throw "Phase 2 regression gate failed." }
    & $Python -m compileall -q src/workspace src/webapi citeprism_api.py tests/test_phase3_auth.py tests/test_phase3_workspace.py
    if ($LASTEXITCODE -ne 0) { throw "Phase 3 Python compilation failed." }
    & $Python -m unittest tests.test_phase3_auth tests.test_phase3_workspace -v
    if ($LASTEXITCODE -ne 0) { throw "Phase 3 workflow tests failed." }
    & $Python -c "from src.webapi.app import app; assert app.openapi()['info']['version'] == '1.0.0'"
    if ($LASTEXITCODE -ne 0) { throw "OpenAPI contract generation failed." }
    Push-Location web
    try {
        & npm ci
        if ($LASTEXITCODE -ne 0) { throw "npm clean install failed." }
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw "React production build failed." }
    } finally { Pop-Location }
    & git diff --check
    if ($LASTEXITCODE -ne 0) { throw "Whitespace verification failed." }
    Write-Host "Phase 3 verification passed. Inspect skipped PostgreSQL tests if -WithPostgres was not supplied."
} finally { Pop-Location }
