#!/usr/bin/env python3
"""Jarvis diagnostics: blocked tasks + WorldView/FINNHUB config check."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TextIO


Output = Callable[[str], None]
_TERMINAL_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def terminal_safe(value: object) -> str:
    """Strip terminal control characters from untrusted diagnostic fields."""
    return _TERMINAL_CONTROL_RE.sub("", str(value)).replace("\x1b", "")


def resolve_repo_root() -> Path:
    """Resolve the repository containing this diagnostics script."""
    return Path(__file__).resolve().parent


def _blocked_task_label(task: Mapping[str, object]) -> tuple[str, str]:
    task_id = terminal_safe(task.get("id") or task.get("session_name") or "?")
    title = terminal_safe(task.get("title") or task.get("task") or "?")
    return task_id, title


def print_blocked_tasks(base: Path, *, output: Output = print) -> None:
    output("=" * 60)
    output("BLOCKED TASKS")
    output("=" * 60)
    try:
        tasks = json.loads((base / "WORK_QUEUE.json").read_text(encoding="utf-8"))
        blocked = [
            task
            for task in tasks
            if str(task.get("status", "")).lower() == "blocked"
        ]
        output(f"Total tasks: {len(tasks)}   Blocked: {len(blocked)}")
        output("")
        for task in blocked[:20]:
            task_id, title = _blocked_task_label(task)
            output(f"  [{task_id}] {title[:60]}")
            reason = task.get("blocked_reason") or task.get("reason")
            if reason:
                output(f"       Reason: {terminal_safe(reason)}")
            if task.get("depends_on"):
                output(f"       Depends on: {terminal_safe(task['depends_on'])}")
        if len(blocked) > 20:
            output(f"  ... and {len(blocked) - 20} more")
    except (OSError, TypeError, ValueError) as exc:
        output(f"Could not read WORK_QUEUE.json: {exc}")


def _read_config(path: Path, *, output: Output) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        output(f"Could not read config {path}: {exc}")
        return None


def print_worldview_config(
    base: Path,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    output: Output = print,
) -> None:
    home = Path.home() if home is None else home
    environ = os.environ if environ is None else environ

    output("")
    output("=" * 60)
    output("WORLDVIEW / FINNHUB CONFIG")
    output("=" * 60)

    config_files = [
        base / "config.py",
        base / ".env",
        base / "WorldView" / ".env",
        base / "WorldView" / "config.py",
        home / "WorldView" / ".env",
        home / "WorldView" / "config.py",
        home / ".env",
    ]
    worldview_dirs = [base / "WorldView", home / "WorldView"]

    for directory in worldview_dirs:
        if directory.exists():
            output(f"Found WorldView dir: {directory}")
            for path in list(directory.iterdir())[:15]:
                output(f"  {path.name}")
            output("")

    for config_file in config_files:
        if not config_file.exists():
            continue
        content = _read_config(config_file, output=output)
        if content is None:
            continue
        if "finnhub" in content.lower():
            output(f"FINNHUB found in: {config_file}")
        else:
            output(f"Config exists (no FINNHUB): {config_file}")

    key_is_set = bool(environ.get("FINNHUB_API_KEY", ""))
    output("")
    output(f"FINNHUB_API_KEY in environment: {'SET' if key_is_set else 'NOT SET'}")

    output("")
    output("Searching for .env files in jarvis-ai...")
    for path in base.rglob(".env"):
        output(f"  Found: {path}")
        content = _read_config(path, output=output)
        if content is not None and "finnhub" in content.lower():
            output("    -> Contains FINNHUB configuration")


def run_diagnostics(base: Path) -> None:
    print_blocked_tasks(base)
    print_worldview_config(base)


def pause_if_requested(
    requested: bool,
    *,
    stdin: TextIO | None = None,
) -> None:
    stream = sys.stdin if stdin is None else stdin
    if not requested or not stream.isatty():
        return
    try:
        input("\nPress Enter to close...")
    except (EOFError, OSError):
        pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pause",
        action="store_true",
        help="wait for Enter before closing when standard input is a TTY",
    )
    args = parser.parse_args(argv)

    base = resolve_repo_root()
    run_diagnostics(base)
    pause_if_requested(args.pause)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
