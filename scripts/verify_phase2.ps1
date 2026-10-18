[CmdletBinding()]
param(
    [string]$Python = ".\.venv-phase1\Scripts\python.exe",
    [switch]$WithPostgres
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
$names = @("CITEPRISM_TEST_ADMIN_DATABASE_URL", "CITEPRISM_TEST_DATABASE_URL", "CITEPRISM_TEST_RUNTIME_ROLE")
$previous = @{}
foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, "Process") }
try {
    & $Python -c "import sys; assert (3, 11) <= sys.version_info[:2] < (3, 14), 'Use Python 3.11, 3.12 or 3.13'"
    if ($LASTEXITCODE -ne 0) { throw "Python check failed. Pass -Python with the environment interpreter path." }
    if ($WithPostgres) {
        & docker compose --project-directory . -f deploy/compose/compose.trust.yml up -d --wait
        if ($LASTEXITCODE -ne 0) { throw "Development PostgreSQL did not start." }
        $env:CITEPRISM_TEST_ADMIN_DATABASE_URL = "postgresql://citeprism_admin:citeprism-admin-dev-only@127.0.0.1:55432/citeprism_trust_dev"
        $env:CITEPRISM_TEST_DATABASE_URL = "postgresql://citeprism_app:citeprism-app-dev-only@127.0.0.1:55432/citeprism_trust_dev"
        $env:CITEPRISM_TEST_RUNTIME_ROLE = "citeprism_app"
        & $Python -m src.trust.cli migrate --database-url $env:CITEPRISM_TEST_ADMIN_DATABASE_URL --runtime-role citeprism_app
        if ($LASTEXITCODE -ne 0) { throw "Migration failed. The app has not been started." }
    }
    & $Python -m compileall -q config database src ui tests scripts database_manager.py pipeline_orchestrator.py streamlit_app.py
    if ($LASTEXITCODE -ne 0) { throw "Compilation failed." }
    if ($WithPostgres) { & $Python .\scripts\verify_phase2.py --require-postgres }
    else { & $Python .\scripts\verify_phase2.py }
    if ($LASTEXITCODE -ne 0) { throw "Phase 2 verification failed. Do not commit this release." }
    & git diff --check
    if ($LASTEXITCODE -ne 0) { throw "Whitespace check failed." }
    Write-Host "Phase 2 checks passed. Start the app with the same Python interpreter."
} finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name], "Process") }
    Pop-Location
}
