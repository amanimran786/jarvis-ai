#!/bin/bash
cd "$(dirname "$0")"

LOG_FILE=".jarvis_crash.log"
MAX_RESTARTS="${JARVIS_MAX_RESTARTS:-5}"
RESTART_DELAY="${JARVIS_RESTART_DELAY_SECONDS:-2}"
attempt=0

while true; do
  venv/bin/python main.py "$@"
  code=$?
  if [ "$code" -eq 0 ]; then
    exit 0
  fi
  attempt=$((attempt + 1))
  printf '[%s] main.py exited with code %s; restart attempt %s/%s in %s seconds\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$code" "$attempt" "$MAX_RESTARTS" "$RESTART_DELAY" >> "$LOG_FILE"
  if [ "$attempt" -ge "$MAX_RESTARTS" ]; then
    printf '[%s] Max restart attempts reached; exiting.\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
    exit "$code"
  fi
  sleep "$RESTART_DELAY"
  if [ "$RESTART_DELAY" -lt 30 ]; then
    RESTART_DELAY=$((RESTART_DELAY * 2))
  fi
done
