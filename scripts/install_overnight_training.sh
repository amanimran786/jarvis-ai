#!/bin/bash
# Install Jarvis quiet overnight worker via launchd
#
# Usage: bash scripts/install_overnight_training.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_FILE="$SCRIPT_DIR/jarvis.overnight-training.plist"
WORKER_FILE="$SCRIPT_DIR/jarvis_overnight_worker.sh"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

if [ ! -f "$PLIST_FILE" ]; then
    echo "Error: plist file not found at $PLIST_FILE"
    exit 1
fi
if [ ! -f "$WORKER_FILE" ]; then
    echo "Error: worker script not found at $WORKER_FILE"
    exit 1
fi

# Ensure LaunchAgents directory exists
mkdir -p "$LAUNCH_AGENTS_DIR"

DEST="$LAUNCH_AGENTS_DIR/jarvis.overnight-training.plist"
LABEL="ai.jarvis.overnight-training"
DOMAIN="gui/$(id -u)"

# Copy plist to LaunchAgents
chmod +x "$WORKER_FILE"
cp "$PLIST_FILE" "$DEST"
echo "Copied plist to $DEST"

# Reload idempotently. bootout fails when the job is not loaded, which is fine.
launchctl bootout "$DOMAIN" "$DEST" >/dev/null 2>&1 || true
if launchctl bootstrap "$DOMAIN" "$DEST"; then
    launchctl enable "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
    echo "Loaded $LABEL with launchctl bootstrap"
else
    echo "bootstrap failed; trying legacy launchctl load"
    launchctl load "$DEST"
fi

echo ""
echo "Installed successfully! Quiet overnight Jarvis work runs nightly at 11pm."
echo "It does not open Terminal or steal focus; inspect it with logs."
echo ""
echo "Check status:"
echo "  launchctl list | grep jarvis"
echo "  bash scripts/overnight_training_status.sh"
echo ""
echo "View logs:"
echo "  tail -f ~/Library/Logs/jarvis-overnight-worker.log"
echo "  tail -f ~/Library/Logs/jarvis-overnight-worker-error.log"
echo "  tail -f ~/Library/Logs/jarvis-training.log"
echo "  tail -f ~/Library/Logs/jarvis-training-error.log"
echo ""
echo "Uninstall (if needed):"
echo "  launchctl unload ~/Library/LaunchAgents/jarvis.overnight-training.plist"
