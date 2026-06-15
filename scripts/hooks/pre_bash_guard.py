#!/usr/bin/env python3
"""PreToolUse hook — block dangerous Bash patterns before execution.

Gates shell commands that match known-destructive or irreversible patterns.
Exit code 2 = BLOCK (Claude Code aborts the tool call and shows the reason).
Exit code 0 = allow.
"""
from __future__ import annotations

import json
import re
import sys

# Patterns that always require explicit human approval.
# These are checked against the full command string.
_BLOCK_PATTERNS: list[tuple[str, str]] = [
    (r"\bgit\s+push\s+.*--force\b", "force-push blocked — confirm manually if intentional"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard blocked — destructive, confirm manually"),
    (r"\brm\s+-rf\s+/", "rm -rf / blocked — catastrophic"),
    (r"\bchmod\s+777\b", "chmod 777 blocked — overly permissive"),
    (r"\bdrop\s+table\b", "DROP TABLE blocked — confirm manually"),
    (r"\btruncate\s+table\b", "TRUNCATE TABLE blocked — confirm manually"),
    (r"\bgit\s+push\b.*\bmain\b.*--force", "force-push to main blocked"),
    (r":\s*>\s*/etc/", "overwrite of /etc/ file blocked"),
    (r"\bkill\s+-9\s+1\b", "kill -9 1 (PID 1) blocked"),
]

# Patterns that are suspicious but allowed with a logged warning.
_WARN_PATTERNS: list[tuple[str, str]] = [
    (r"\bgit\s+push\b(?!.*--dry-run)", "git push — make sure this is intentional"),
    (r"\bpip\s+install\b.*--break-system-packages", "pip --break-system-packages detected"),
    (r"\bcurl\b.*\|\s*bash\b", "curl-pipe-bash pattern — review the URL"),
    (r"\bwget\b.*\|\s*bash\b", "wget-pipe-bash pattern — review the URL"),
]


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    if payload.get("tool_name") != "Bash":
        sys.exit(0)

    cmd = (payload.get("tool_input") or {}).get("command", "")
    if not cmd:
        sys.exit(0)

    for pattern, reason in _BLOCK_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            print(f"[pre-bash-guard] BLOCKED: {reason}", flush=True)
            print(f"  Command: {cmd[:200]}", flush=True)
            sys.exit(2)  # Blocks the tool call.

    for pattern, warning in _WARN_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            print(f"[pre-bash-guard] ⚠  {warning}", flush=True)
            print(f"  Command: {cmd[:200]}", flush=True)
            # Don't exit 2 — just warn, allow.

    sys.exit(0)


if __name__ == "__main__":
    main()
