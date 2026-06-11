#!/usr/bin/env bash
# Start the Jarvis API server on a fixed port, killing any prior instance first.
# Usage: ./scripts/run_api.sh [port]
set -euo pipefail

PORT="${1:-8765}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE="/tmp/jarvis_api.pid"
LOG="/tmp/jarvis_api.log"

# Kill prior instance
if [ -f "$PIDFILE" ]; then
  OLD=$(cat "$PIDFILE")
  kill "$OLD" 2>/dev/null && echo "[run_api] killed prior PID $OLD" || true
  rm -f "$PIDFILE"
fi
# Also clear the port in case something else holds it
lsof -ti :"$PORT" 2>/dev/null | xargs kill -9 2>/dev/null || true
sleep 1

cd "$REPO"
# Load .env if present (sets JARVIS_API_TOKEN etc.)
if [ -f .env ]; then
  set -o allexport
  source .env
  set +o allexport
fi

if [ -z "${JARVIS_API_TOKEN:-}" ]; then
  JARVIS_API_TOKEN="$(python - <<'PYEOF'
import secrets
print(secrets.token_urlsafe(24))
PYEOF
)"
  export JARVIS_API_TOKEN
  echo "[run_api] generated an ephemeral JARVIS_API_TOKEN for this process"
fi

python - <<PYEOF > "$LOG" 2>&1 &
import os, uvicorn
os.environ.setdefault('JARVIS_API_TOKEN', os.getenv('JARVIS_API_TOKEN', ''))
import api
api._API_TOKEN = os.environ['JARVIS_API_TOKEN']
api._host = '0.0.0.0'
api._port = int(os.getenv('JARVIS_API_PORT', '$PORT'))
uvicorn.run(api.app, host='0.0.0.0', port=api._port, log_level='warning')
PYEOF

echo $! > "$PIDFILE"
echo "[run_api] PID $(cat $PIDFILE) -- waiting for port $PORT..."

for i in $(seq 1 15); do
  sleep 1
  if lsof -ti :"$PORT" > /dev/null 2>&1; then
    echo "[run_api] Server up on http://0.0.0.0:$PORT"
    echo "[run_api] Dashboard: http://localhost:$PORT/dashboard"
    echo '[run_api] Use Authorization: Bearer $JARVIS_API_TOKEN for protected endpoints'
    exit 0
  fi
done

echo "[run_api] ERROR: port $PORT not bound after 15s -- check $LOG"
tail -20 "$LOG"
exit 1
