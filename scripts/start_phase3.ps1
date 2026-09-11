[CmdletBinding()]
param(
    [string]$Python = ".\.venv-phase3\Scripts\python.exe",
    [int]$ApiPort = 8000,
    [int]$WebPort = 5173
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
$api = $null
try {
    & $Python -c "import fastapi, uvicorn, multipart; from src.webapi.app import create_app; create_app()"
    if ($LASTEXITCODE -ne 0) { throw "Phase 3 Python dependencies or configuration are invalid." }
    $api = Start-Process -FilePath $Python -ArgumentList @(
        "-m", "uvicorn", "citeprism_api:app", "--host", "127.0.0.1", "--port", "$ApiPort"
    ) -NoNewWindow -PassThru
    Start-Sleep -Seconds 2
    if ($api.HasExited) { throw "The CitePrism API did not start. Inspect the preceding error." }
    Push-Location web
    try {
        $env:VITE_CITEPRISM_API = "http://127.0.0.1:$ApiPort/api/v1"
        & npm run dev -- --port $WebPort
        if ($LASTEXITCODE -ne 0) { throw "The React workspace stopped with an error." }
    } finally { Pop-Location }
} finally {
    if ($api -and -not $api.HasExited) { Stop-Process -Id $api.Id }
    Pop-Location
}
