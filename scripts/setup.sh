#!/usr/bin/env bash
# CitePrism setup for macOS and Linux. On Windows use scripts/setup_phase4.ps1.
#
#   ./scripts/setup.sh                    # Python environment, web app, .env, signing key
#   ./scripts/setup.sh --with-embeddings  # also local sentence embeddings (adds PyTorch, ~2 GB)
#   ./scripts/setup.sh --python python3.12
set -euo pipefail
cd "$(dirname "$0")/.."

usage() { sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; }

WITH_EMBEDDINGS=0
PYTHON=""
while [ $# -gt 0 ]; do
  case "$1" in
    --with-embeddings) WITH_EMBEDDINGS=1 ;;
    --python) PYTHON="${2:?--python needs a value}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

supported() { "$1" -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)' 2>/dev/null; }

if [ -z "$PYTHON" ]; then
  for candidate in python3.12 python3.13 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && supported "$candidate"; then PYTHON="$candidate"; break; fi
  done
fi
if [ -z "$PYTHON" ] || ! supported "$PYTHON"; then
  echo "CitePrism needs Python 3.11, 3.12 or 3.13 (3.12 recommended)." >&2
  echo "  macOS:          brew install python@3.12" >&2
  echo "  Ubuntu/Debian:  sudo apt install python3.12 python3.12-venv" >&2
  exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "Node.js 20 or newer (with npm) is needed for the web app: https://nodejs.org/" >&2
  exit 1
fi
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
if [ "$NODE_MAJOR" -lt 20 ]; then
  echo "Node.js $NODE_MAJOR is too old; install Node.js 20 or newer: https://nodejs.org/" >&2
  exit 1
fi

echo "Using $("$PYTHON" --version) and Node.js $(node --version)."
if [ ! -x .venv-phase4/bin/python ]; then
  "$PYTHON" -m venv .venv-phase4 || {
    echo "Could not create the Python environment. On Ubuntu/Debian: sudo apt install python3.12-venv" >&2
    exit 1
  }
fi
VENV_PY=.venv-phase4/bin/python

"$VENV_PY" -m pip install --upgrade pip
"$VENV_PY" -m pip install -r requirements/requirements-phase4.txt -r requirements/requirements-dev.txt
if [ "$WITH_EMBEDDINGS" -eq 1 ]; then
  "$VENV_PY" -m pip install -r requirements/requirements-embeddings.txt
fi

if [ ! -f .env ]; then
  cp env.example .env
  echo "Created .env from env.example. Add your own API keys to it (see README, step 4)."
fi

# Each installation signs its exports with its own key. It never leaves this machine
# (secrets/ is ignored by git).
KEY=secrets/export-signing.key
if [ -f "$KEY" ]; then
  echo "An export signing key already exists; leaving it untouched."
else
  "$VENV_PY" -c "from src.workspace.signing import Signer; print('Export signing key written to', Signer.generate_key('$KEY'))"
fi
if ! grep -Eq '^[[:space:]]*CITEPRISM_EXPORT_SIGNING_KEY=' .env; then
  printf '\nCITEPRISM_EXPORT_SIGNING_KEY=%s\n' "$KEY" >> .env
fi

(cd web && npm ci)

cat <<'EOF'

Setup finished.
  1. Put your own API keys in .env (README, step 4).
  2. Check the installation:  ./scripts/verify.sh
  3. Start CitePrism:         ./scripts/start.sh   then open http://127.0.0.1:5173

OCR for scanned PDFs is optional: install tesseract and set CITEPRISM_ENABLE_OCR=true in .env.
EOF
