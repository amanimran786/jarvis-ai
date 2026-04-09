#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_APP="$ROOT/dist/Jarvis.app"
TARGET_APP="/Users/truthseeker/Applications/Jarvis.app"

cd "$ROOT"

./venv/bin/python -m PyInstaller --noconfirm --clean Jarvis.spec

rm -rf "$TARGET_APP"
cp -R "$DIST_APP" "$TARGET_APP"

echo "Installed Jarvis.app to $TARGET_APP"
