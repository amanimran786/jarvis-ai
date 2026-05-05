#!/bin/bash
# Install Jarvis overnight training scheduler via launchd
#
# Usage: bash scripts/install_overnight_training.sh

set -e

PLIST_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/jarvis.overnight-training.plist"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

if [ ! -f "$PLIST_FILE" ]; then
    echo "Error: plist file not found at $PLIST_FILE"
    exit 1
fi

# Ensure LaunchAgents directory exists
mkdir -p "$LAUNCH_AGENTS_DIR"

# Copy plist to LaunchAgents
cp "$PLIST_FILE" "$LAUNCH_AGENTS_DIR/"
echo "Copied plist to $LAUNCH_AGENTS_DIR/jarvis.overnight-training.plist"

# Load the plist
launchctl load "$LAUNCH_AGENTS_DIR/jarvis.overnight-training.plist"
echo "Loaded plist with launchctl"

echo ""
echo "Installed successfully! Training runs nightly at 11pm."
echo ""
echo "Check status:"
echo "  launchctl list | grep jarvis"
echo ""
echo "View logs:"
echo "  tail -f ~/Library/Logs/jarvis-training.log"
echo "  tail -f ~/Library/Logs/jarvis-training-error.log"
echo ""
echo "Uninstall (if needed):"
echo "  launchctl unload ~/Library/LaunchAgents/jarvis.overnight-training.plist"
