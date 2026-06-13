#!/bin/bash
# Install Jarvis continuous upgrade scout via launchd.
#
# Usage: bash scripts/install_upgrade_loop.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_FILE="$SCRIPT_DIR/jarvis.upgrade-loop.plist"
WORKER_FILE="$SCRIPT_DIR/jarvis_upgrade_worker.sh"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

if [ ! -f "$PLIST_FILE" ]; then
    echo "Error: plist file not found at $PLIST_FILE"
    exit 1
fi
if [ ! -f "$WORKER_FILE" ]; then
    echo "Error: worker script not found at $WORKER_FILE"
    exit 1
fi

mkdir -p "$LAUNCH_AGENTS_DIR"

DEST="$LAUNCH_AGENTS_DIR/jarvis.upgrade-loop.plist"
LABEL="ai.jarvis.upgrade-loop"
DOMAIN="gui/$(id -u)"

chmod +x "$WORKER_FILE"
cp "$PLIST_FILE" "$DEST"
echo "Copied plist to $DEST"

launchctl bootout "$DOMAIN" "$DEST" >/dev/null 2>&1 || true
if launchctl bootstrap "$DOMAIN" "$DEST"; then
    launchctl enable "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
    echo "Loaded $LABEL with launchctl bootstrap"
else
    echo "bootstrap failed; trying legacy launchctl load"
    launchctl load "$DEST"
fi

echo ""
echo "Installed successfully. Jarvis will check upgrade sources every 6 hours."
echo "It creates local preview tickets only; human approval is still required for execution."
echo ""
echo "Check status:"
echo "  launchctl list | grep jarvis.upgrade-loop"
echo ""
echo "View logs:"
echo "  tail -f ~/Library/Logs/jarvis-upgrade-loop.log"
echo "  tail -f ~/Library/Logs/jarvis-upgrade-loop-error.log"
echo ""
echo "Run once manually:"
echo "  bash scripts/jarvis_upgrade_worker.sh"
echo ""
echo "Uninstall (if needed):"
echo "  launchctl bootout $DOMAIN ~/Library/LaunchAgents/jarvis.upgrade-loop.plist"
