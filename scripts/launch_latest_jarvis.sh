#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APPLICATIONS_APP="$HOME/Applications/Jarvis.app"
INSTALL_SCRIPT="$ROOT/scripts/install_jarvis_app.sh"
STAMP_FILE="$ROOT/.jarvis_build_stamp"
LOCKFILE="/tmp/jarvis_launcher.lock"

# Ensure only one launcher invocation at a time (prevent race conditions)
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  # Another launcher is already running, wait briefly then exit
  sleep 1
  exit 0
fi

latest_source_stamp() {
  /usr/bin/find "$ROOT" \
    \( -path "$ROOT/venv" -o -path "$ROOT/dist" -o -path "$ROOT/build" -o -path "$ROOT/.git" \) -prune -o \
    -type f \
    \( -name '*.py' -o -name '*.sh' -o -name '*.md' -o -name '*.json' -o -name '*.toml' -o -name '*.spec' -o -name '*.png' -o -name '*.icns' -o -name '*.svg' \) \
    -exec stat -f '%m' {} + | sort -nr | head -1
}

needs_rebuild() {
  if [[ ! -d "$APPLICATIONS_APP" ]]; then
    return 0
  fi

  local current_source
  current_source="$(latest_source_stamp)"
  local current_stamp=""
  if [[ -f "$STAMP_FILE" ]]; then
    current_stamp="$(cat "$STAMP_FILE" 2>/dev/null || true)"
  fi

  if [[ -z "$current_stamp" || "$current_stamp" != "$current_source" ]]; then
    return 0
  fi

  return 1
}

if needs_rebuild; then
  "$INSTALL_SCRIPT" --applications-only
fi

# Use 'open -a' (without -n) to reuse existing instance if running
# or start a new one if not running (normal macOS behavior)
open -a "$APPLICATIONS_APP"
