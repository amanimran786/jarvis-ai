#!/bin/bash
# Show the health of Jarvis overnight local fine-tuning automation.

set -euo pipefail

REPO_DIR="${JARVIS_REPO_DIR:-/Users/truthseeker/jarvis-ai}"
PYTHON="$REPO_DIR/venv/bin/python3"
LABEL="ai.jarvis.overnight-training"
STDOUT_LOG="$HOME/Library/Logs/jarvis-training.log"
STDERR_LOG="$HOME/Library/Logs/jarvis-training-error.log"

cd "$REPO_DIR"

echo "Launchd:"
if launchctl list | grep -q "$LABEL"; then
  launchctl list | grep "$LABEL"
else
  echo "  $LABEL is not loaded"
fi

echo
echo "Scheduler:"
"$PYTHON" -m local_runtime.local_finetune_scheduler status

echo
echo "Recent stdout log:"
if [[ -f "$STDOUT_LOG" ]]; then
  tail -n 20 "$STDOUT_LOG"
else
  echo "  No stdout log found at $STDOUT_LOG"
fi

echo
echo "Recent stderr log:"
if [[ -f "$STDERR_LOG" ]]; then
  tail -n 20 "$STDERR_LOG"
else
  echo "  No stderr log found at $STDERR_LOG"
fi
