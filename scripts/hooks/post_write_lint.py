#!/usr/bin/env python3
"""PostToolUse hook — ruff lint on every Python file write/edit.

Runs ruff check on the changed file and prints a compact summary.
Non-blocking (exit 0) — it's advisory, not a gate.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _read_payload() -> dict:
    try:
        return json.loads(sys.stdin.read())
    except Exception:
        return {}


def _file_path(payload: dict) -> Path | None:
    tool_input = payload.get("tool_input") or {}
    fp = tool_input.get("file_path") or tool_input.get("path") or ""
    if not fp:
        return None
    p = Path(fp)
    if not p.is_absolute():
        p = REPO_ROOT / p
    return p if p.exists() and p.suffix == ".py" else None


def main() -> None:
    payload = _read_payload()
    if payload.get("tool_name") not in ("Edit", "Write"):
        return

    path = _file_path(payload)
    if path is None:
        return

    result = subprocess.run(
        ["python3", "-m", "ruff", "check", "--quiet", "--output-format=concise", str(path)],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(REPO_ROOT),
    )
    output = (result.stdout + result.stderr).strip()
    if not output:
        return  # Clean — stay quiet.

    rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
    lines = output.splitlines()
    shown = lines[:10]
    truncated = len(lines) - len(shown)

    print(f"\n[lint-hook] {rel}", flush=True)
    for ln in shown:
        print(f"  {ln}", flush=True)
    if truncated > 0:
        print(f"  … {truncated} more (run: ruff check {rel})", flush=True)


if __name__ == "__main__":
    main()
