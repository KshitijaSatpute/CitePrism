[CmdletBinding()]
param(
    [string]$Python = ".\.venv-phase4\Scripts\python.exe",
    [switch]$WithPostgres,
    [switch]$SkipFrontend
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    # Phase 3 remains the regression gate: Phase 4 must not break it.
    & "$PSScriptRoot\verify_phase3.ps1" -Python $Python -WithPostgres:$WithPostgres
    if ($LASTEXITCODE -ne 0) { throw "Phase 3 regression gate failed." }

    & $Python -m compileall -q config database src scripts tests citeprism_api.py `
        database_manager.py pipeline_orchestrator.py streamlit_app.py
    if ($LASTEXITCODE -ne 0) { throw "Phase 4 Python compilation failed." }

    & $Python -c "from config.settings import Config; Config.validate_phase4(); print('configuration valid')"
    if ($LASTEXITCODE -ne 0) { throw "Phase 4 configuration validation failed." }

    & $Python .\scripts\check_no_secrets.py
    if ($LASTEXITCODE -ne 0) { throw "Secrets are tracked by git. Remove and rotate them before continuing." }

    & $Python -m unittest tests.test_v2_requirements tests.test_assistant tests.test_provider_fallback tests.test_coverage_providers tests.test_author_year tests.test_book_references tests.test_chat_breadth tests.test_injection_benchmark tests.test_data_policy tests.test_conversation tests.test_hardening tests.test_review_fixes tests.test_real_report_542 -v
    if ($LASTEXITCODE -ne 0) { throw "CitePrism V2.0 requirement tests failed." }

    & $Python -m unittest `
        tests.test_phase4_contracts `
        tests.test_phase4_workspace `
        tests.test_phase4_runtime `
        tests.test_phase4_evidence `
        tests.test_phase4_api -v
    if ($LASTEXITCODE -ne 0) { throw "Phase 4 tests failed." }

    & $Python -c "from src.webapi.app import app; s=app.openapi(); assert '/api/v1/telemetry/slos' in s['paths']; assert '/api/v1/audits/{audit_id}/repairs' in s['paths']; print('OpenAPI contract present')"
    if ($LASTEXITCODE -ne 0) { throw "Phase 4 OpenAPI contract generation failed." }

    if ($WithPostgres) {
        Write-Host "Starting the disposable workspace database..."
        & docker compose --project-directory . -f deploy/compose/compose.phase4.yml up -d --wait workspace-db
        if ($LASTEXITCODE -ne 0) { throw "The workspace PostgreSQL container did not start." }
        $env:CITEPRISM_TEST_WORKSPACE_ADMIN_URL = "postgresql://citeprism_admin:citeprism-admin-dev-only@127.0.0.1:55433/citeprism_workspace"
        $env:CITEPRISM_TEST_WORKSPACE_URL = "postgresql://citeprism_workspace_app:citeprism-workspace-dev-only@127.0.0.1:55433/citeprism_workspace"
        & $Python .\scripts\migrate_workspace.py --database-url $env:CITEPRISM_TEST_WORKSPACE_ADMIN_URL --runtime-role citeprism_workspace_app
        if ($LASTEXITCODE -ne 0) { throw "Workspace migrations failed." }
        & $Python -m unittest tests.test_phase4_postgres -v
        if ($LASTEXITCODE -ne 0) { throw "PostgreSQL workspace tests failed." }
    }

    if (-not $SkipFrontend) {
        Push-Location web
        try {
            & npm ci
            if ($LASTEXITCODE -ne 0) { throw "npm clean install failed." }
            & npm run build
            if ($LASTEXITCODE -ne 0) { throw "React production build failed." }
        } finally { Pop-Location }
    }

    & git diff --check
    if ($LASTEXITCODE -ne 0) { throw "Whitespace verification failed." }

    Write-Host ""
    Write-Host "Phase 4 verification passed."
    if (-not $WithPostgres) {
        Write-Host "PostgreSQL workspace tests were skipped. Rerun with -WithPostgres before a release."
    }
} finally { Pop-Location }
