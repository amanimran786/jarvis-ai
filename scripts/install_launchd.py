#!/usr/bin/env python3
"""Install the Jarvis Dashboard launchd service from the checked-in plist.

Copies scripts/com.jarvis.dashboard.plist into ~/Library/LaunchAgents and
(re)bootstraps it via `launchctl bootstrap` (the modern replacement for the
deprecated `launchctl load`).
"""
import pathlib
import shutil
import subprocess
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE_PLIST = REPO_ROOT / "scripts" / "com.jarvis.dashboard.plist"
LOGS_DIR = REPO_ROOT / "logs"
LAUNCH_AGENTS = pathlib.Path.home() / "Library" / "LaunchAgents"
DEST_PLIST = LAUNCH_AGENTS / "com.jarvis.dashboard.plist"
LABEL = "com.jarvis.dashboard"


def main() -> int:
    if not SOURCE_PLIST.exists():
        print(f"ERROR: source plist not found: {SOURCE_PLIST}")
        return 1

    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    uid = subprocess.run(
        ["id", "-u"], capture_output=True, text=True, check=True
    ).stdout.strip()
    domain = f"gui/{uid}"

    # Bootout any existing instance first (ignore errors if not loaded).
    subprocess.run(
        ["launchctl", "bootout", f"{domain}/{LABEL}"],
        capture_output=True, text=True,
    )

    # bootout is asynchronous — launchd hasn't necessarily released the label
    # by the time the command returns. Bootstrapping too soon fails with
    # "Bootstrap failed: 5: Input/output error". Poll until the service is
    # actually gone before reinstalling.
    for _ in range(20):
        still_present = subprocess.run(
            ["launchctl", "print", f"{domain}/{LABEL}"],
            capture_output=True, text=True,
        ).returncode == 0
        if not still_present:
            break
        time.sleep(0.25)

    shutil.copyfile(SOURCE_PLIST, DEST_PLIST)
    DEST_PLIST.chmod(0o644)
    print(f"Installed plist: {DEST_PLIST}")

    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(DEST_PLIST)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"launchctl bootstrap failed: {result.stderr.strip()}")
        return 1

    print("SUCCESS: Jarvis Dashboard launchd service installed.")
    print("It will auto-start on login and restart if it exits.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
