#!/usr/bin/env python3
"""Quick maintenance: chmod +x restart_jarvis.command, then git commit new files."""
import subprocess, os, pathlib

BASE = pathlib.Path.home() / "jarvis-ai"
os.chdir(BASE)

# 1. Fix execute permission on restart_jarvis.command
cmd_file = BASE / "restart_jarvis.command"
if cmd_file.exists():
    subprocess.run(["chmod", "+x", str(cmd_file)])
    print(f"chmod +x {cmd_file.name} ✅")
else:
    print("restart_jarvis.command not found (skipping)")

# 2. Git status
result = subprocess.run(["git", "status", "--short"], capture_output=True, text=True, cwd=BASE)
print("\n=== git status ===")
print(result.stdout or "(nothing to show)")

# 3. Git add all jarvis-ai tracked/new files
subprocess.run(["git", "add", "-A"], cwd=BASE)

# 4. Git commit
commit_msg = "feat: CODEX-5 launchd wiring, CODEX-6 history CLI, fix restart permissions"
result = subprocess.run(
    ["git", "commit", "-m", commit_msg],
    capture_output=True, text=True, cwd=BASE
)
print("\n=== git commit ===")
print(result.stdout)
if result.returncode != 0:
    print("STDERR:", result.stderr)
else:
    print("Committed ✅")

input("\nPress Enter to close...")
