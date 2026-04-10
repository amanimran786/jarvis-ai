#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_APP="$ROOT/dist/Jarvis.app"
APPLICATIONS_APP="$HOME/Applications/Jarvis.app"
DESKTOP_APP="$HOME/Desktop/Jarvis.app"
STAMP_FILE="$ROOT/.jarvis_build_stamp"
APPLICATIONS_ONLY=0

if [[ "${1:-}" == "--applications-only" ]]; then
  APPLICATIONS_ONLY=1
fi

cd "$ROOT"

./venv/bin/python -m PyInstaller --noconfirm --clean Jarvis.spec

install_copy() {
  local target="$1"
  rm -rf "$target"
  cp -R "$DIST_APP" "$target"
  xattr -cr "$target" || true
  touch "$target" || true
}

install_copy "$APPLICATIONS_APP"

latest_source_stamp() {
  /usr/bin/find "$ROOT" \
    \( -path "$ROOT/venv" -o -path "$ROOT/dist" -o -path "$ROOT/build" -o -path "$ROOT/.git" \) -prune -o \
    -type f \
    \( -name '*.py' -o -name '*.sh' -o -name '*.md' -o -name '*.json' -o -name '*.toml' -o -name '*.spec' -o -name '*.png' -o -name '*.icns' -o -name '*.svg' \) \
    -exec stat -f '%m' {} + | sort -nr | head -1
}

create_desktop_launcher() {
  rm -rf "$DESKTOP_APP"
  mkdir -p "$DESKTOP_APP/Contents/MacOS" "$DESKTOP_APP/Contents/Resources"
  cp "$ROOT/assets/jarvis.icns" "$DESKTOP_APP/Contents/Resources/jarvis.icns"
  cat > "$DESKTOP_APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleDisplayName</key>
  <string>Jarvis</string>
  <key>CFBundleExecutable</key>
  <string>Jarvis</string>
  <key>CFBundleIconFile</key>
  <string>jarvis.icns</string>
  <key>CFBundleIdentifier</key>
  <string>com.truthseeker.jarvis.launcher</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>Jarvis</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>LSMinimumSystemVersion</key>
  <string>12.0</string>
  <key>LSUIElement</key>
  <true/>
  <key>NSHighResolutionCapable</key>
  <true/>
</dict>
</plist>
PLIST
  cat > "$DESKTOP_APP/Contents/MacOS/Jarvis" <<EOF
#!/usr/bin/env bash
exec "$ROOT/scripts/launch_latest_jarvis.sh"
EOF
  chmod +x "$DESKTOP_APP/Contents/MacOS/Jarvis"
  xattr -cr "$DESKTOP_APP" || true
  touch "$DESKTOP_APP" || true
}

printf '%s\n' "$(latest_source_stamp)" > "$STAMP_FILE"

if [[ "$APPLICATIONS_ONLY" -eq 0 ]]; then
  create_desktop_launcher
fi

/usr/bin/qlmanage -r cache >/dev/null 2>&1 || true

echo "Installed Jarvis.app to:"
echo "  $APPLICATIONS_APP"
if [[ "$APPLICATIONS_ONLY" -eq 0 ]]; then
  echo "  $DESKTOP_APP"
else
  echo "  (Desktop launcher unchanged)"
fi
