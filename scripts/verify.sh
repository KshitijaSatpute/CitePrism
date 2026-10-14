#!/usr/bin/env bash
# Check a CitePrism installation on macOS or Linux: compile, configuration, secret scan,
# the whole test suite and the web app's production build.
# On Windows use scripts/verify_phase4.ps1.
#
#   ./scripts/verify.sh
#   ./scripts/verify.sh --skip-frontend
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv-phase4/bin/python
SKIP_FRONTEND=0
while [ $# -gt 0 ]; do
  case "$1" in
    --python) PY="${2:?}"; shift ;;
    --skip-frontend) SKIP_FRONTEND=1 ;;
    -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
if [ ! -x "$PY" ]; then
  echo "No Python environment at $PY. Run ./scripts/setup.sh first." >&2
  exit 1
fi

step() { printf '\n== %s\n' "$1"; }

step "Compiling"
"$PY" -m compileall -q config database src ui scripts tests citeprism_api.py database_manager.py \
  pipeline_orchestrator.py streamlit_app.py

step "Configuration"
"$PY" -c "from config.settings import Config; Config.validate_phase4(); print('configuration valid')"

if [ -d .git ]; then
  step "No secrets tracked by git"
  "$PY" scripts/check_no_secrets.py
fi

step "Tests (PostgreSQL and live-provider tests skip without them)"
"$PY" -m unittest discover -s tests -t .

step "API contract"
"$PY" -c "from src.webapi.app import app; s = app.openapi(); assert '/api/v1/audits/{audit_id}/repairs' in s['paths']; print('OpenAPI contract present')"

if [ "$SKIP_FRONTEND" -eq 0 ]; then
  step "Web app production build"
  (cd web && npm ci && npm run build)
fi

if [ -d .git ]; then
  git diff --check
fi

printf '\nVerification passed.\n'
