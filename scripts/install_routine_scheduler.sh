#!/bin/bash
# Install or uninstall the Jarvis routine scheduler launchd agent.
# Usage:
#   ./scripts/install_routine_scheduler.sh          # install and load
#   ./scripts/install_routine_scheduler.sh --unload # unload and remove

set -euo pipefail

PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/jarvis.routine-scheduler.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/ai.jarvis.routine-scheduler.plist"
LABEL="ai.jarvis.routine-scheduler"

if [[ "${1:-}" == "--unload" ]]; then
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
    rm -f "$PLIST_DEST"
    echo "routine-scheduler unloaded and removed"
    exit 0
fi

cp "$PLIST_SRC" "$PLIST_DEST"
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load "$PLIST_DEST"
echo "routine-scheduler loaded (label: $LABEL)"
echo "Logs: ~/Library/Logs/jarvis-routine-scheduler.log"
