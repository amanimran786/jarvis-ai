#!/bin/bash
cd "$(dirname "$0")"

LOG_FILE=".jarvis_crash.log"

while true; do
  venv/bin/python main.py "$@"
  code=$?
  if [ "$code" -eq 0 ]; then
    exit 0
  fi
  printf '[%s] main.py exited with code %s; restarting in 2 seconds\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$code" >> "$LOG_FILE"
  sleep 2
done
