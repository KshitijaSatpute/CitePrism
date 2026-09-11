[CmdletBinding()]
param(
    [string]$PythonLauncher = "py",
    [string]$PythonVersion = "3.12",
    [switch]$GenerateSigningKey
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    if ($PythonLauncher -eq "py") { & py "-$PythonVersion" -m venv .venv-phase4 }
    else { & $PythonLauncher -m venv .venv-phase4 }
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Python environment." }

    $python = Join-Path $root ".venv-phase4\Scripts\python.exe"
    & $python -m pip install --upgrade pip
    & $python -m pip install -r requirements-phase4.txt -r requirements-dev.txt
    if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed." }

    if (-not (Test-Path .env)) { Copy-Item env.example .env }

    if ($GenerateSigningKey) {
        $keyDirectory = Join-Path $root "secrets"
        New-Item -ItemType Directory -Force -Path $keyDirectory | Out-Null
        $keyPath = Join-Path $keyDirectory "export-signing.key"
        if (Test-Path $keyPath) {
            Write-Host "An export signing key already exists; leaving it untouched."
        } else {
            & $python -c "from src.workspace.signing import Signer; print(Signer.generate_key(r'$keyPath'))"
            if ($LASTEXITCODE -ne 0) { throw "Could not generate the export signing key." }
            Write-Host "Export signing key written to $keyPath"
            Write-Host "Set CITEPRISM_EXPORT_SIGNING_KEY=$keyPath in .env, and keep this file out of version control."
        }
    }

    Push-Location web
    try { & npm ci; if ($LASTEXITCODE -ne 0) { throw "Frontend dependency installation failed." } }
    finally { Pop-Location }

    Write-Host ""
    Write-Host "Phase 4 setup finished."
    Write-Host "  1. Configure provider credentials in .env."
    Write-Host "  2. For the durable runtime, start PostgreSQL and MinIO:"
    Write-Host "       docker compose -f compose.phase4.yml up -d workspace-db objects provision"
    Write-Host "     then set CITEPRISM_WORKSPACE_BACKEND=postgres in .env."
    Write-Host "  3. Verify:  .\scripts\verify_phase4.ps1 -Python .\.venv-phase4\Scripts\python.exe"
    Write-Host "  4. Start:   .\scripts\start_phase4.ps1 -Python .\.venv-phase4\Scripts\python.exe"
    Write-Host ""
    Write-Host "OCR is optional. To enable it, install tesseract and set CITEPRISM_ENABLE_OCR=true."
} finally { Pop-Location }
