#!/usr/bin/env python3
"""Organize jarvis-ai: move one-time installer/maintenance scripts to scripts/ subfolder."""
import shutil, pathlib

BASE = pathlib.Path.home() / "jarvis-ai"
SCRIPTS = BASE / "scripts"
SCRIPTS.mkdir(exist_ok=True)

# Scripts that are one-time installers or maintenance utilities
# (not user-facing CLIs that should stay in root)
TO_MOVE = [
    "install_launchd.py",    # one-time launchd installer
    "setup_plugins.py",      # one-time plugin system installer
    "git_commit.py",         # maintenance commit helper
]

moved = []
skipped = []

for name in TO_MOVE:
    src = BASE / name
    dst = SCRIPTS / name
    if src.exists():
        shutil.move(str(src), str(dst))
        moved.append(name)
        print(f"  Moved  {name} → scripts/{name}")
    else:
        skipped.append(name)
        print(f"  Skip   {name} (not found)")

# Write a README for the scripts folder
(SCRIPTS / "README.md").write_text("""# scripts/

One-time installers and maintenance utilities for jarvis-ai.

| Script | Purpose |
|--------|---------|
| install_launchd.py | Installs Jarvis dashboard as a macOS launchd service |
| setup_plugins.py   | Sets up the CODEX-7 plugin system (base class, loader, examples) |
| git_commit.py      | Commits pending changes with a standard message |

## User-facing CLIs (in root)

- `history_cli.py`   — Browse task history in the terminal
- `plugin_manager.py` — List, enable, disable, test plugins
""")

print()
print(f"Done! Moved {len(moved)} script(s) to jarvis-ai/scripts/")
print()
print("Root now contains only core files and user-facing tools.")
print("One-time installers are in jarvis-ai/scripts/")

input("\nPress Enter to close...")
