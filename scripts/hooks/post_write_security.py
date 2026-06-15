#!/usr/bin/env python3
"""PostToolUse hook — security scan on every file write/edit.

Claude Code calls this after any Edit or Write tool use. The hook receives
a JSON payload on stdin. It re-reads the changed file and runs the security
checklist from .claude/skills/jarvis-security.md.

Exit code 0 = scan clean or non-Python file (no-op).
Exit code 2 = BLOCK (only valid for PreToolUse; here we just warn loudly).
Stdout is shown in the Claude Code interface as a hook result.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _read_payload() -> dict:
    try:
        return json.loads(sys.stdin.read())
    except Exception:
        return {}


def _file_path_from_payload(payload: dict) -> Path | None:
    tool_input = payload.get("tool_input") or {}
    fp = tool_input.get("file_path") or tool_input.get("path") or ""
    if not fp:
        return None
    p = Path(fp)
    if not p.is_absolute():
        p = REPO_ROOT / p
    return p if p.exists() else None


CHECKS: list[tuple[str, str, str]] = [
    # (pattern, flag, human label)
    (r"subprocess\.run\(.*shell\s*=\s*True", "CRITICAL", "subprocess shell=True"),
    (r"\bos\.system\s*\(", "CRITICAL", "os.system() call"),
    (r"(?<!\w)eval\s*\(", "HIGH", "eval() call"),
    (r"(?<!\w)exec\s*\(", "HIGH", "exec() call"),
    (r"pickle\.load\s*\(", "HIGH", "pickle.load() deserialization"),
    (r"yaml\.load\s*\([^,)]+\)", "HIGH", "yaml.load() without Loader"),
    (r'(?i)(API_KEY|SECRET|PASSWORD|TOKEN)\s*=\s*["\'][^"\']{4}', "CRITICAL", "hardcoded secret"),
    (r"(?i)sk-[a-zA-Z0-9]{20,}", "CRITICAL", "OpenAI key pattern"),
    (r"(?i)gh[pousr]_[a-zA-Z0-9]{36}", "CRITICAL", "GitHub token pattern"),
    (r"AKIA[A-Z0-9]{16}", "CRITICAL", "AWS access key"),
    (r"\.\.[\\/]", "MEDIUM", "potential path traversal"),
]

SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}


def scan_file(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    findings: list[dict] = []
    for pattern, severity, label in CHECKS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line):
                # Skip commented-out lines.
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                findings.append({
                    "severity": severity,
                    "label": label,
                    "line": i,
                    "snippet": line.rstrip()[:120],
                })
    return sorted(findings, key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))


def main() -> None:
    payload = _read_payload()
    tool_name = payload.get("tool_name", "")
    if tool_name not in ("Edit", "Write", "NotebookEdit"):
        return

    path = _file_path_from_payload(payload)
    if path is None or path.suffix not in (".py", ".sh", ".yaml", ".yml", ".json", ".env"):
        return

    findings = scan_file(path)
    if not findings:
        # Quiet on clean.
        return

    rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
    criticals = [f for f in findings if f["severity"] == "CRITICAL"]
    highs = [f for f in findings if f["severity"] == "HIGH"]
    mediums = [f for f in findings if f["severity"] == "MEDIUM"]

    lines = [f"\n[security-hook] {rel}  —  {len(findings)} finding(s)"]
    for f in findings:
        icon = "🚨" if f["severity"] == "CRITICAL" else ("⚠️" if f["severity"] == "HIGH" else "ℹ️")
        lines.append(f"  {icon} [{f['severity']}] line {f['line']}: {f['label']}")
        lines.append(f"     {f['snippet']}")

    if criticals:
        lines.append(f"\n  STOP — {len(criticals)} CRITICAL issue(s). Fix before committing.")
    elif highs:
        lines.append(f"\n  {len(highs)} HIGH-severity issue(s). Review before committing.")

    print("\n".join(lines), flush=True)

    # Exit 2 on CRITICAL so Claude Code surfaces it prominently.
    if criticals:
        sys.exit(2)


if __name__ == "__main__":
    main()
