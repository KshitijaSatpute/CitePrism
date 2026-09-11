[CmdletBinding()]
param(
    [string]$Python = ".\.venv-phase4\Scripts\python.exe",
    [int]$ApiPort = 8000,
    [int]$WebPort = 5173,
    [int]$Workers = 0
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
$api = $null
$workerProcesses = @()
try {
    & $Python -c "from config.settings import Config; Config.validate_phase4(); import fastapi, uvicorn, multipart; from src.webapi.app import create_app"
    if ($LASTEXITCODE -ne 0) { throw "Phase 4 dependencies or configuration are invalid." }

    $backend = & $Python -c "from config.settings import Config; print(Config.WORKSPACE_BACKEND)"
    if ($backend -eq "postgres") {
        Write-Host "Applying workspace migrations..."
        & $Python .\scripts\migrate_workspace.py
        if ($LASTEXITCODE -ne 0) { throw "Workspace migrations failed. The application has not started." }
    }

    $api = Start-Process -FilePath $Python -ArgumentList @(
        "-m", "uvicorn", "citeprism_api:app", "--host", "127.0.0.1", "--port", "$ApiPort"
    ) -NoNewWindow -PassThru
    Start-Sleep -Seconds 2
    if ($api.HasExited) { throw "The CitePrism API did not start. Inspect the preceding error." }

    # Separate worker processes are the durable runtime. Without -Workers the
    # API runs an in-process worker, which is the single-workstation profile.
    for ($index = 1; $index -le $Workers; $index++) {
        $workerProcesses += Start-Process -FilePath $Python -ArgumentList @(
            ".\scripts\worker.py", "--worker-id", "local-worker-$index"
        ) -NoNewWindow -PassThru
        Write-Host "Started worker $index."
    }

    Push-Location web
    try {
        $env:VITE_CITEPRISM_API = "http://127.0.0.1:$ApiPort/api/v1"
        & npm run dev -- --port $WebPort
        if ($LASTEXITCODE -ne 0) { throw "The React workspace stopped with an error." }
    } finally { Pop-Location }
} finally {
    foreach ($worker in $workerProcesses) {
        if ($worker -and -not $worker.HasExited) { Stop-Process -Id $worker.Id }
    }
    if ($api -and -not $api.HasExited) { Stop-Process -Id $api.Id }
    Pop-Location
}
