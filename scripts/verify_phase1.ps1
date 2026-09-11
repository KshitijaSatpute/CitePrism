[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$WithPostgres
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
$testVariables = @("CITEPRISM_TEST_ADMIN_DATABASE_URL", "CITEPRISM_TEST_DATABASE_URL", "CITEPRISM_TEST_RUNTIME_ROLE")
$previousEnvironment = @{}
foreach ($name in $testVariables) { $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process") }
try {
    & $Python -c "import sys; assert (3, 11) <= sys.version_info[:2] < (3, 14), 'Use Python 3.11, 3.12, or 3.13'"
    if ($LASTEXITCODE -ne 0) { throw "Supported Python not available; pass -Python with your new environment interpreter path." }
    if ($WithPostgres) {
        & docker compose -f compose.trust.yml up -d --wait
        if ($LASTEXITCODE -ne 0) { throw "Development PostgreSQL did not start." }
        $env:CITEPRISM_TEST_ADMIN_DATABASE_URL = "postgresql://citeprism_admin:citeprism-admin-dev-only@127.0.0.1:55432/citeprism_trust_dev"
        $env:CITEPRISM_TEST_DATABASE_URL = "postgresql://citeprism_app:citeprism-app-dev-only@127.0.0.1:55432/citeprism_trust_dev"
        $env:CITEPRISM_TEST_RUNTIME_ROLE = "citeprism_app"
        & $Python -m src.trust.cli migrate --database-url $env:CITEPRISM_TEST_ADMIN_DATABASE_URL --runtime-role citeprism_app
        if ($LASTEXITCODE -ne 0) { throw "Trust migration failed. Do not continue or commit." }
    }
    & $Python -m compileall -q config database src ui tests scripts database_manager.py pipeline_orchestrator.py streamlit_app.py
    if ($LASTEXITCODE -ne 0) { throw "Python compilation failed." }
    & $Python -m unittest discover -v
    if ($LASTEXITCODE -ne 0) { throw "Regression tests failed. Do not commit." }
    & git diff --check
    if ($LASTEXITCODE -ne 0) { throw "Working-tree whitespace check failed." }
    & git diff --cached --check
    if ($LASTEXITCODE -ne 0) { throw "Staged whitespace check failed." }
    Write-Host "Verification passed. Inspect any SKIPPED tests above before accepting the release."
    if (-not $WithPostgres) { Write-Warning "No database was started. Use -WithPostgres for the live database acceptance gate." }
} finally {
    foreach ($name in $testVariables) { [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], "Process") }
    Pop-Location
}
