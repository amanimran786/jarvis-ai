"""
workspace_context.py — Workspace-aware coding context for Jarvis.

snapshot() returns a compact dict describing the current repo state:
  git_status, recent_commits, directory_tree, key_files

Cached for 60 seconds so rapid queries don't re-run git. Callers
inject the snapshot into the system prompt for terminal, self_improve,
and code_task routing.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent
_CACHE_TTL = 60.0

_lock = threading.Lock()
_cache: dict | None = None
_cache_ts: float = 0.0


def _run(args: list[str], cwd: Path | None = None, timeout: int = 8) -> str:
    try:
        return subprocess.check_output(
            args, cwd=str(cwd or _ROOT), stderr=subprocess.DEVNULL,
            text=True, timeout=timeout,
        ).strip()
    except Exception:
        return ""


def _git_status() -> str:
    return _run(["git", "status", "--short"])


def _recent_commits(n: int = 5) -> str:
    return _run(["git", "log", f"--oneline", f"-{n}"])


def _dir_tree(max_depth: int = 2) -> str:
    lines: list[str] = []
    try:
        for p in sorted(_ROOT.rglob("*")):
            depth = len(p.relative_to(_ROOT).parts)
            if depth > max_depth:
                continue
            if any(part.startswith(".") or part in ("__pycache__", "node_modules", ".git") for part in p.parts):
                continue
            indent = "  " * (depth - 1)
            suffix = "/" if p.is_dir() else ""
            lines.append(f"{indent}{p.name}{suffix}")
    except Exception:
        pass
    return "\n".join(lines[:120])  # cap output size


def _key_files() -> list[str]:
    important = [
        "config.py", "router.py", "model_router.py", "orchestrator.py",
        "task_planner.py", "coder_workbench.py", "workspace_context.py",
        "main.py", "ui.py", "voice.py",
    ]
    return [f for f in important if (_ROOT / f).exists()]


def snapshot() -> dict:
    """Return workspace snapshot dict, cached for 60 seconds."""
    global _cache, _cache_ts
    now = time.monotonic()
    with _lock:
        if _cache is not None and (now - _cache_ts) < _CACHE_TTL:
            return _cache
        _cache = {
            "git_status": _git_status(),
            "recent_commits": _recent_commits(),
            "directory_tree": _dir_tree(),
            "key_files": _key_files(),
            "root": str(_ROOT),
        }
        _cache_ts = now
        return _cache


def format_for_prompt(snap: dict | None = None) -> str:
    """Format snapshot as a compact system-prompt block."""
    s = snap or snapshot()
    parts = [f"## Workspace: {s.get('root', '')}"]
    if s.get("git_status"):
        parts.append(f"Git status:\n{s['git_status']}")
    if s.get("recent_commits"):
        parts.append(f"Recent commits:\n{s['recent_commits']}")
    if s.get("key_files"):
        parts.append("Key files: " + ", ".join(s["key_files"]))
    return "\n\n".join(parts)


def invalidate() -> None:
    """Force cache refresh on next call (e.g. after a file write)."""
    global _cache
    with _lock:
        _cache = None
