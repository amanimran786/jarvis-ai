"""
ade/session.py — tmux session management.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def available() -> bool:
    return shutil.which("tmux") is not None


def create(
    session_name: str,
    worktree_path: Path,
    loop_script: Path,
    task_name: str,
    prompt: str,
    repo_root: Path,
) -> None:
    """Spawn a detached tmux session running the ade-loop script."""
    if not available():
        raise RuntimeError("tmux not found — run: brew install tmux")
    if exists(session_name):
        raise RuntimeError(f"session '{session_name}' already exists; use 'ade watch {task_name}'")

    cmd = [
        "tmux", "new-session", "-d",
        "-s", session_name,
        "-c", str(worktree_path),
        sys.executable, str(loop_script),
        "--task", task_name,
        "--prompt", prompt,
        "--worktree", str(worktree_path),
        "--repo-root", str(repo_root),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"tmux new-session failed: {result.stderr.strip()}")


def attach(session_name: str) -> None:
    """Replace the current process with an attached tmux session."""
    subprocess.run(["tmux", "attach-session", "-t", session_name])


def kill(session_name: str) -> None:
    subprocess.run(
        ["tmux", "kill-session", "-t", session_name],
        capture_output=True,
    )


def exists(session_name: str) -> bool:
    r = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    return r.returncode == 0


def list_sessions() -> list[str]:
    r = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return []
    return [s.strip() for s in r.stdout.splitlines() if s.strip()]


def session_pid(session_name: str) -> int | None:
    """Return the PID of the first window's active pane, or None."""
    r = subprocess.run(
        ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_pid}"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        try:
            return int(line.strip())
        except ValueError:
            pass
    return None
