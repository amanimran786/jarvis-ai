#!/usr/bin/env python3
"""Install Jarvis Dashboard as a launchd service that auto-starts on login."""
import subprocess, os, pathlib

JARVIS_DIR = pathlib.Path.home() / "jarvis-ai"
LOGS_DIR = JARVIS_DIR / "logs"
LAUNCH_AGENTS = pathlib.Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = LAUNCH_AGENTS / "com.jarvis.dashboard.plist"

# Ensure directories exist
LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

PLIST_CONTENT = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.jarvis.dashboard</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/python3</string>
        <string>{JARVIS_DIR}/kill_and_start.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{JARVIS_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{LOGS_DIR}/launchd.log</string>
    <key>StandardErrorPath</key>
    <string>{LOGS_DIR}/launchd_error.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>
</dict>
</plist>"""

# Unload if already loaded (ignore errors)
subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)

# Write the plist
PLIST_PATH.write_text(PLIST_CONTENT)
print(f"Wrote plist: {PLIST_PATH}")

# Load the service
result = subprocess.run(["launchctl", "load", str(PLIST_PATH)], capture_output=True, text=True)
if result.returncode == 0:
    print("SUCCESS: Jarvis Dashboard launchd service installed!")
    print("It will now auto-start on every login.")
else:
    print(f"launchctl load output: {result.stdout}")
    print(f"launchctl load error: {result.stderr}")
    # Try bootstrap method (macOS 10.15+)
    uid = os.getuid()
    result2 = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(PLIST_PATH)],
        capture_output=True, text=True
    )
    if result2.returncode == 0:
        print("SUCCESS via bootstrap!")
    else:
        print(f"Bootstrap also failed: {result2.stderr}")

input("\nPress Enter to close...")
