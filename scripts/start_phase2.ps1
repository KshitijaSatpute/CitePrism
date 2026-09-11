[CmdletBinding()]
param([string]$Python = ".\.venv-phase1\Scripts\python.exe", [int]$Port = 8502)
$ErrorActionPreference = "Stop"
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    & $Python -c "import streamlit; from google import genai; import requests, pypdf"
    if ($LASTEXITCODE -ne 0) { throw "Install requirements-phase2.txt into the selected Python environment first." }
    & $Python -m streamlit run streamlit_app.py --server.port $Port --server.address 127.0.0.1
    if ($LASTEXITCODE -ne 0) { throw "CitePrism stopped with an error. Inspect the preceding message." }
} finally { Pop-Location }
