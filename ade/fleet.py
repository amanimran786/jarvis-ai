"""
ade/fleet.py — Concurrent fleet orchestration for ADE.

Manages multiple parallel ADE sessions (git worktrees + tmux panes),
each working on a different task. Provides a dashboard of running tasks
and lets you teleport a failed task to another worker.

Usage:
    python -m ade.fleet status
    python -m ade.fleet start --task TASK_ID --prompt "..." [--worker N]
    python -m ade.fleet teleport --task TASK_ID [--to-worker N]
    python -m ade.fleet cancel --task TASK_ID
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ade import state as st

_FLEET_ROOT = Path(__file__).parent.parent / "runtime" / "fleet"
_REPO_ROOT = Path(__file__).parent.parent


# ── Worker pool ───────────────────────────────────────────────────────────────

def _list_workers() -> list[dict]:
    """Return active fleet workers from state files."""
    workers = []
    if not _FLEET_ROOT.exists():
        return workers
    for f in sorted(_FLEET_ROOT.glob("worker-*.json")):
        try:
            data = json.loads(f.read_text())
            workers.append(data)
        except (OSError, json.JSONDecodeError):
            continue
    return workers


def _save_worker(worker_id: str, task_id: str, task_name: str, worktree: str, tmux_session: str) -> None:
    _FLEET_ROOT.mkdir(parents=True, exist_ok=True)
    path = _FLEET_ROOT / f"worker-{worker_id}.json"
    path.write_text(json.dumps({
        "worker_id": worker_id,
        "task_id": task_id,
        "task_name": task_name,
        "worktree": worktree,
        "tmux_session": tmux_session,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "running",
    }, indent=2))


def _delete_worker(worker_id: str) -> None:
    path = _FLEET_ROOT / f"worker-{worker_id}.json"
    path.unlink(missing_ok=True)


def _next_worker_id() -> str:
    workers = _list_workers()
    existing_ids = {int(w["worker_id"]) for w in workers if w["worker_id"].isdigit()}
    for n in range(1, 100):
        if n not in existing_ids:
            return str(n)
    return str(len(workers) + 1)


# ── Worktree helpers ──────────────────────────────────────────────────────────

def _create_worktree(task_id: str, worker_id: str) -> Path | None:
    """Create a git worktree for an isolated task. Returns path or None if not a git repo."""
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        shell=False,
    )
    if result.returncode != 0:
        return None

    worktree_path = _FLEET_ROOT / f"worktree-{worker_id}-{task_id[:8]}"
    branch_name = f"ade/{task_id[:16]}-worker{worker_id}"

    subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, str(worktree_path)],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        shell=False,
    )
    return worktree_path if worktree_path.exists() else None


def _remove_worktree(worktree_path: str) -> None:
    p = Path(worktree_path)
    if p.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(p)],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            shell=False,
        )


# ── Tmux helpers ──────────────────────────────────────────────────────────────

def _tmux_available() -> bool:
    return subprocess.run(["which", "tmux"], capture_output=True).returncode == 0


def _launch_tmux_session(session_name: str, cmd: list[str], cwd: str) -> bool:
    """Create a new detached tmux session running cmd. Returns True on success."""
    if not _tmux_available():
        return False
    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-c", cwd,
         "--", *cmd],
        capture_output=True,
        shell=False,
    )
    return result.returncode == 0


def _kill_tmux_session(session_name: str) -> None:
    subprocess.run(
        ["tmux", "kill-session", "-t", session_name],
        capture_output=True,
        shell=False,
    )


# ── Fleet commands ────────────────────────────────────────────────────────────

def cmd_status(args: argparse.Namespace) -> None:
    """Print fleet status dashboard."""
    workers = _list_workers()
    if not workers:
        print("[fleet] No active workers.")
        return

    print(f"\n{'WORKER':<8} {'TASK':<20} {'STATUS':<14} {'STARTED':<22} {'WORKTREE'}")
    print("-" * 90)
    for w in workers:
        task_status = _get_task_status(w.get("task_name", ""), w.get("worktree", ""))
        print(
            f"{w['worker_id']:<8} "
            f"{w['task_name'][:19]:<20} "
            f"{task_status:<14} "
            f"{w['started_at'][:19]:<22} "
            f"{w['worktree']}"
        )
    print()


def cmd_start(args: argparse.Namespace) -> None:
    """Start a new worker on a task."""
    worker_id = _next_worker_id()
    task_id = args.task
    prompt = args.prompt

    # Try to create a git worktree; fall back to repo root copy
    worktree = _create_worktree(task_id, worker_id)
    if worktree is None:
        worktree = _REPO_ROOT
        print(f"[fleet] Not a git repo — running in {worktree} (no worktree isolation)")
    else:
        print(f"[fleet] Created worktree: {worktree}")

    session_name = f"jarvis-ade-w{worker_id}"
    ade_cmd = [
        sys.executable, "-m", "ade.loop",
        "--task", task_id,
        "--prompt", prompt,
        "--worktree", str(worktree),
        "--repo-root", str(_REPO_ROOT),
    ]

    launched = _launch_tmux_session(session_name, ade_cmd, str(worktree))
    if not launched:
        print(f"[fleet] tmux not available — run manually:")
        print(f"  {' '.join(ade_cmd)}")
    else:
        print(f"[fleet] Worker {worker_id} started in tmux session '{session_name}'")
        print(f"[fleet] Attach with: tmux attach -t {session_name}")

    _save_worker(worker_id, task_id, task_id[:20], str(worktree), session_name)


def cmd_teleport(args: argparse.Namespace) -> None:
    """Teleport a failed task to a new worker."""
    from infra import checkpointer

    task_id = args.task
    checkpoint = checkpointer.load(task_id)
    if checkpoint is None:
        print(f"[fleet] No checkpoint found for task {task_id!r}. Cannot teleport.")
        return

    target_worker = args.to_worker
    print(f"[fleet] Teleporting task {task_id} (retry #{checkpoint.retry_count + 1})…")

    result = checkpointer.teleport(checkpoint, target_worker=target_worker)
    if result.get("ok"):
        print(f"[fleet] Task re-queued on event bus. Stream ID: {result.get('stream_id', 'N/A')}")
    else:
        print(f"[fleet] Teleport failed: {result.get('error', 'unknown error')}")
        print(f"[fleet] Start a new worker manually with: ade fleet start --task {task_id} --prompt '{checkpoint.title}'")


def cmd_cancel(args: argparse.Namespace) -> None:
    """Cancel a running worker and clean up its worktree."""
    task_id = args.task
    workers = _list_workers()
    matched = [w for w in workers if w["task_id"] == task_id or w["worker_id"] == task_id]

    if not matched:
        print(f"[fleet] No worker found for task {task_id!r}.")
        return

    for w in matched:
        print(f"[fleet] Cancelling worker {w['worker_id']} (task: {w['task_name']})…")
        _kill_tmux_session(w["tmux_session"])
        _remove_worktree(w["worktree"])
        _delete_worker(w["worker_id"])
        st.set_status(_REPO_ROOT, w["task_name"], st.FAILED)
        print(f"[fleet] Worker {w['worker_id']} cancelled.")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_task_status(task_name: str, worktree: str) -> str:
    """Read task status from the ADE shared state file."""
    try:
        entry = st.get(_REPO_ROOT, task_name)
        return entry.get("status", "unknown") if entry else "unknown"
    except Exception:
        return "unknown"


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="ADE fleet orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)

    # status
    sub.add_parser("status", help="Show fleet status dashboard")

    # start
    p_start = sub.add_parser("start", help="Start a new worker on a task")
    p_start.add_argument("--task", required=True, help="Task ID")
    p_start.add_argument("--prompt", required=True, help="Task prompt")
    p_start.add_argument("--worker", help="Worker ID override (default: auto)")

    # teleport
    p_tp = sub.add_parser("teleport", help="Teleport failed task to new worker")
    p_tp.add_argument("--task", required=True, help="Task ID to teleport")
    p_tp.add_argument("--to-worker", default=None, help="Target worker ID (optional)")

    # cancel
    p_cancel = sub.add_parser("cancel", help="Cancel a running worker")
    p_cancel.add_argument("--task", required=True, help="Task or worker ID to cancel")

    args = parser.parse_args()
    commands = {
        "status": cmd_status,
        "start": cmd_start,
        "teleport": cmd_teleport,
        "cancel": cmd_cancel,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
