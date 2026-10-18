[CmdletBinding()]
param([string]$PythonLauncher = "py", [string]$PythonVersion = "3.12")
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    if ($PythonLauncher -eq "py") { & py "-$PythonVersion" -m venv .venv-phase3 }
    else { & $PythonLauncher -m venv .venv-phase3 }
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Python environment." }
    $python = Join-Path $root ".venv-phase3\Scripts\python.exe"
    & $python -m pip install --upgrade pip
    & $python -m pip install -r requirements/requirements-phase3.txt -r requirements/requirements-dev.txt
    if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed." }
    if (-not (Test-Path .env)) { Copy-Item env.example .env }
    Push-Location web
    try { & npm ci; if ($LASTEXITCODE -ne 0) { throw "Frontend dependency installation failed." } }
    finally { Pop-Location }
    Write-Host "Phase 3 setup finished. Configure provider credentials in .env, then run scripts\start_phase3.ps1."
} finally { Pop-Location }
