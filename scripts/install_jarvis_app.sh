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
  # Re-sign copied app bundle to keep a consistent Team ID across nested dylibs.
  codesign --force --deep --sign - "$target" >/dev/null 2>&1 || true
  touch "$target" || true
}

install_copy "$APPLICATIONS_APP"

latest_source_stamp() {
  /usr/bin/find "$ROOT" \
    \( -path "$ROOT/venv" -o -path "$ROOT/dist" -o -path "$ROOT/build" -o -path "$ROOT/.git" -o -path "$ROOT/memory" -o -path "$ROOT/graphify-out" -o -path "$ROOT/.jarvis_backups" \) -prune -o \
    -type f \
    \( -name '*.py' -o -name '*.spec' -o -path "$ROOT/scripts/*.sh" -o -name 'jarvis.icns' -o -name 'jarvis.png' \) \
    -exec stat -f '%m' {} + | sort -nr | head -1
}

create_desktop_alias() {
  local candidate="" target="" resolved_target="" resolved_app=""
  resolved_app="$(/usr/bin/realpath "$APPLICATIONS_APP" 2>/dev/null || printf '%s' "$APPLICATIONS_APP")"
  while IFS= read -r -d '' candidate; do
    [[ -e "$candidate" || -L "$candidate" ]] || continue
    if [[ "$candidate" == "$DESKTOP_APP" ]]; then
      continue
    fi
    if [[ -L "$candidate" ]]; then
      target="$(readlink "$candidate")"
      if [[ "$target" != /* ]]; then
        target="$(cd "$(dirname "$candidate")" && cd "$(dirname "$target")" && pwd -P)/$(basename "$target")"
      fi
      resolved_target="$(/usr/bin/realpath "$target" 2>/dev/null || printf '%s' "$target")"
    else
      resolved_target="$(/usr/bin/realpath "$candidate" 2>/dev/null || printf '%s' "$candidate")"
    fi
    if [[ "$resolved_target" == "$resolved_app" ]]; then
      rm -f "$candidate"
    fi
  done < <(/usr/bin/find "$HOME/Desktop" -maxdepth 1 -name 'Jarvis*.app' -print0)
  rm -rf "$DESKTOP_APP"
  ln -s "$APPLICATIONS_APP" "$DESKTOP_APP"
  touch -h "$DESKTOP_APP" 2>/dev/null || true
}

printf '%s\n' "$(latest_source_stamp)" > "$STAMP_FILE"

# Always keep Desktop pointed at the exact same app bundle in ~/Applications so
# launch behavior and icon identity are consistent.
create_desktop_alias

/usr/bin/qlmanage -r cache >/dev/null 2>&1 || true

echo "Installed Jarvis.app to:"
echo "  $APPLICATIONS_APP"
echo "Desktop shortcut:"
echo "  $DESKTOP_APP -> $APPLICATIONS_APP"
