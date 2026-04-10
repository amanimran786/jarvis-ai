from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import runtime_state


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return cleaned[:40] or "task"


def repo_root(start: str | Path | None = None) -> Path | None:
    base = Path(start or Path.cwd()).resolve()
    proc = _run_git(["rev-parse", "--show-toplevel"], base)
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip())


def worktree_base_dir() -> Path:
    base = runtime_state.app_data_dir() / "worktrees"
    base.mkdir(parents=True, exist_ok=True)
    return base


def prepare_isolated_workspace(task_id: str, prompt: str, *, enabled: bool = True) -> dict[str, Any]:
    if not enabled:
        return {"ok": False, "enabled": False, "created": False, "reason": "disabled"}

    root = repo_root()
    if root is None:
        return {"ok": False, "enabled": True, "created": False, "reason": "not_git_repo"}

    branch = f"codex/{task_id}-{_slug(prompt)}"
    target = worktree_base_dir() / task_id
    if target.exists():
        return {
            "ok": True,
            "enabled": True,
            "created": False,
            "reason": "already_exists",
            "repo_root": str(root),
            "worktree_path": str(target),
            "branch": branch,
        }

    proc = _run_git(["worktree", "add", "-b", branch, str(target), "HEAD"], root)
    if proc.returncode != 0:
        return {
            "ok": False,
            "enabled": True,
            "created": False,
            "reason": (proc.stderr or proc.stdout or "worktree_add_failed").strip(),
            "repo_root": str(root),
            "worktree_path": str(target),
            "branch": branch,
        }

    return {
        "ok": True,
        "enabled": True,
        "created": True,
        "reason": "",
        "repo_root": str(root),
        "worktree_path": str(target),
        "branch": branch,
    }
