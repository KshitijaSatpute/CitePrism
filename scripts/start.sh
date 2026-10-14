#!/usr/bin/env bash
# Start CitePrism on macOS or Linux: the API on port 8000 and the web app on port 5173.
# On Windows use scripts/start_phase4.ps1.
#
#   ./scripts/start.sh
#   ./scripts/start.sh --api-port 8001 --web-port 5174 --workers 2
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv-phase4/bin/python
API_PORT=8000
WEB_PORT=5173
WORKERS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --python) PY="${2:?}"; shift ;;
    --api-port) API_PORT="${2:?}"; shift ;;
    --web-port) WEB_PORT="${2:?}"; shift ;;
    --workers) WORKERS="${2:?}"; shift ;;
    -h|--help) sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ ! -x "$PY" ]; then
  echo "No Python environment at $PY. Run ./scripts/setup.sh first." >&2
  exit 1
fi
"$PY" -c "from config.settings import Config; Config.validate_phase4(); import fastapi, uvicorn, multipart; from src.webapi.app import create_app" \
  || { echo "Dependencies or .env settings are invalid; see the error above." >&2; exit 1; }

if [ "$("$PY" -c 'from config.settings import Config; print(Config.WORKSPACE_BACKEND)')" = "postgres" ]; then
  echo "Applying workspace migrations..."
  "$PY" scripts/migrate_workspace.py
fi

PIDS=()
cleanup() {
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do kill "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT
trap 'exit 130' INT TERM

"$PY" -m uvicorn citeprism_api:app --host 127.0.0.1 --port "$API_PORT" &
PIDS+=("$!")
sleep 2
if ! kill -0 "${PIDS[0]}" 2>/dev/null; then
  echo "The CitePrism API did not start; see the error above." >&2
  exit 1
fi

# Separate workers are the durable runtime. Without --workers the API runs an
# in-process worker, which is the single-computer setup.
i=1
while [ "$i" -le "$WORKERS" ]; do
  "$PY" scripts/worker.py --worker-id "local-worker-$i" &
  PIDS+=("$!")
  echo "Started worker $i."
  i=$((i + 1))
done

echo "CitePrism: http://127.0.0.1:$WEB_PORT   (API docs: http://127.0.0.1:$API_PORT/api/docs)   Ctrl+C stops both."
cd web
VITE_CITEPRISM_API="http://127.0.0.1:$API_PORT/api/v1" npm run dev -- --port "$WEB_PORT"
