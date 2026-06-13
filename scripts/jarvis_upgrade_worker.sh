#!/bin/bash
# Quiet Jarvis upgrade-loop worker.
#
# launchd captures output in ~/Library/Logs/jarvis-upgrade-loop.log. The worker
# only creates local preview tickets and ledger records; it does not dispatch
# agents, merge code, push, deploy, or install packages.

set -euo pipefail

REPO_DIR="${JARVIS_REPO_DIR:-/Users/truthseeker/jarvis-ai}"
PYTHON="${JARVIS_PYTHON:-$REPO_DIR/venv/bin/python3}"
LOG_DIR="$HOME/Library/Logs"
WORKER_LOG="$LOG_DIR/jarvis-upgrade-loop.log"
ERROR_LOG="$LOG_DIR/jarvis-upgrade-loop-error.log"

mkdir -p "$LOG_DIR"
cd "$REPO_DIR"

{
  echo "========================================================================"
  echo "Jarvis upgrade-loop worker"
  echo "Started: $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "Repo: $REPO_DIR"
  echo "PID: $$"
  echo "Mode: local-first watchlist scout; preview tickets only"
  echo "========================================================================"
} >>"$WORKER_LOG"

export HOME="${HOME:-/Users/truthseeker}"
export PATH="$REPO_DIR/venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

run_cmd=(
  "$PYTHON" scripts/upgrade_loop.py
  --fetch-watchlist
  --create-tickets
  --auto-promote
  --limit "${JARVIS_UPGRADE_LIMIT:-10}"
  --source "${JARVIS_UPGRADE_SOURCE:-launchd_watchlist}"
  --json
)

if [[ "${JARVIS_UPGRADE_SANDBOX_SMOKE:-0}" == "1" ]]; then
  run_cmd+=(--sandbox-smoke)
fi

"${run_cmd[@]}" >>"$WORKER_LOG" 2>>"$ERROR_LOG"
status=$?

{
  echo "Finished: $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "Exit code: $status"
  echo
} >>"$WORKER_LOG"

exit "$status"
