#!/usr/bin/env python3
"""Jarvis diagnostics: blocked tasks + WorldView/FINNHUB config check."""
import json, pathlib, os

BASE = pathlib.Path.home() / "jarvis-ai"

# ── 1. Blocked tasks ─────────────────────────────────────────────────
print("=" * 60)
print("BLOCKED TASKS")
print("=" * 60)
try:
    tasks = json.loads((BASE / "WORK_QUEUE.json").read_text())
    blocked = [t for t in tasks if str(t.get("status","")).lower() == "blocked"]
    print(f"Total tasks: {len(tasks)}   Blocked: {len(blocked)}")
    print()
    for t in blocked[:20]:
        print(f"  [{t.get('id','?')}] {t.get('title','?')[:60]}")
        if t.get("blocked_reason") or t.get("reason"):
            print(f"       Reason: {t.get('blocked_reason') or t.get('reason')}")
        if t.get("depends_on"):
            print(f"       Depends on: {t.get('depends_on')}")
    if len(blocked) > 20:
        print(f"  ... and {len(blocked)-20} more")
except Exception as e:
    print(f"Could not read WORK_QUEUE.json: {e}")

# ── 2. WorldView / FINNHUB config ────────────────────────────────────
print()
print("=" * 60)
print("WORLDVIEW / FINNHUB CONFIG")
print("=" * 60)

# Check common config locations
config_files = [
    BASE / "config.py",
    BASE / ".env",
    BASE / "WorldView" / ".env",
    BASE / "WorldView" / "config.py",
    pathlib.Path.home() / "WorldView" / ".env",
    pathlib.Path.home() / "WorldView" / "config.py",
    pathlib.Path.home() / ".env",
]
worldview_dirs = [
    BASE / "WorldView",
    pathlib.Path.home() / "WorldView",
]

for d in worldview_dirs:
    if d.exists():
        print(f"Found WorldView dir: {d}")
        files = list(d.iterdir())[:15]
        for f in files:
            print(f"  {f.name}")
        print()

# Check for FINNHUB in any config
for cfg in config_files:
    if cfg.exists():
        content = cfg.read_text()
        if "FINNHUB" in content.upper() or "finnhub" in content.lower():
            print(f"FINNHUB found in: {cfg}")
            for line in content.splitlines():
                if "finnhub" in line.lower() or "FINNHUB" in line:
                    print(f"  {line.strip()}")
        else:
            print(f"Config exists (no FINNHUB): {cfg}")

# Check env vars
key = os.environ.get("FINNHUB_API_KEY", "")
print(f"\nFINNHUB_API_KEY in environment: {'SET (' + key[:8] + '...)' if key else 'NOT SET'}")

# Check for .env files anywhere in jarvis-ai
print("\nSearching for .env files in jarvis-ai...")
for p in BASE.rglob(".env"):
    print(f"  Found: {p}")
    content = p.read_text()
    if "FINNHUB" in content.upper():
        print("    -> Contains FINNHUB key")

input("\nPress Enter to close...")
