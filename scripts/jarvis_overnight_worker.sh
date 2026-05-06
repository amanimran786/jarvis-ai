#!/bin/bash
# Quiet overnight Jarvis worker.
#
# This is intentionally not a visible Terminal session. launchd captures the
# terminal-style output into ~/Library/Logs/jarvis-overnight-worker.log so the
# Mac can keep being used normally while Jarvis trains in the background.

set -euo pipefail

REPO_DIR="${JARVIS_REPO_DIR:-/Users/truthseeker/jarvis-ai}"
PYTHON="${JARVIS_PYTHON:-$REPO_DIR/venv/bin/python3}"
LOG_DIR="$HOME/Library/Logs"
WORKER_LOG="$LOG_DIR/jarvis-overnight-worker.log"
ERROR_LOG="$LOG_DIR/jarvis-overnight-worker-error.log"

mkdir -p "$LOG_DIR"
cd "$REPO_DIR"

{
  echo "========================================================================"
  echo "Jarvis overnight worker"
  echo "Started: $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "Repo: $REPO_DIR"
  echo "PID: $$"
  echo "Mode: quiet background launchd job; no visible Terminal, no focus steal"
  echo "========================================================================"
} >>"$WORKER_LOG"

export HOME="${HOME:-/Users/truthseeker}"
export JARVIS_TEACHER_CAPTURE="${JARVIS_TEACHER_CAPTURE:-1}"
export JARVIS_OVERNIGHT_QUIET="${JARVIS_OVERNIGHT_QUIET:-1}"
export JARVIS_NO_NOTIFICATION_SOUND="${JARVIS_NO_NOTIFICATION_SOUND:-1}"
export PATH="$REPO_DIR/venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

run_cmd=("$PYTHON" -m local_runtime.local_finetune_scheduler)

if [[ "${JARVIS_OVERNIGHT_PREVENT_SLEEP:-1}" == "1" ]] && command -v caffeinate >/dev/null 2>&1; then
  run_cmd=(/usr/bin/caffeinate -s -t "${JARVIS_OVERNIGHT_MAX_SECONDS:-28800}" "${run_cmd[@]}")
fi

if command -v nice >/dev/null 2>&1; then
  run_cmd=(/usr/bin/nice -n "${JARVIS_OVERNIGHT_NICE:-10}" "${run_cmd[@]}")
fi

"${run_cmd[@]}" >>"$WORKER_LOG" 2>>"$ERROR_LOG"
status=$?

{
  echo "Finished: $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "Exit code: $status"
  echo
} >>"$WORKER_LOG"

exit "$status"
