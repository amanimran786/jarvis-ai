"""
ade/cli.py — Autonomous Dev Environment CLI.

Commands:
  ade start <task_name> [--prompt TEXT]   Create worktree + tmux session
  ade list                                 Show active agents and status
  ade watch <task_name>                    Attach to a session
  ade sync  <task_name>                    Commit worktree + merge to main
  ade stop  <task_name>                    Kill session + remove worktree
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ade import notify, session as sess, state as st

# ── Resolve repo root ─────────────────────────────────────────────────────────

def _repo_root() -> Path:
    try:
        import worktree_manager
        root = worktree_manager.repo_root()
        if root:
            return root
    except ImportError:
        pass
    r = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        return Path(r.stdout.strip())
    return Path.cwd()


def _loop_script() -> Path:
    return Path(__file__).resolve().parents[1] / "scripts" / "ade-loop"


def _slug(name: str) -> str:
    import re
    return re.sub(r"[^a-zA-Z0-9_-]", "-", name).strip("-")[:40]


_SYNC_SCRATCH_PATHS = ("PLAN.md",)


def _git_add_sync_changes(worktree_path: Path) -> None:
    """Stage ADE lane changes without sweeping scratch planning artifacts."""
    cmd = ["git", "add", "-A", "--", "."]
    cmd.extend(f":!{path}" for path in _SYNC_SCRATCH_PATHS)
    subprocess.run(cmd, cwd=str(worktree_path), check=False)


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_start(task_name: str, prompt: str) -> None:
    slug = _slug(task_name)
    session_name = f"ade-{slug}"
    repo_root = _repo_root()
    worktrees_dir = repo_root / ".worktrees"
    worktree_path = worktrees_dir / slug
    branch = f"ade/{slug}"

    # Create worktree
    if worktree_path.exists():
        print(f"[ade] worktree already exists at {worktree_path}")
    else:
        worktrees_dir.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(worktree_path), "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            # Branch may exist — try without -b
            r = subprocess.run(
                ["git", "worktree", "add", str(worktree_path), branch],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                print(f"[ade] ERROR: git worktree add failed:\n{r.stderr}", file=sys.stderr)
                sys.exit(1)
        print(f"[ade] Worktree: {worktree_path}  branch: {branch}")

    # Copy CLAUDE.md
    claude_md = repo_root / "CLAUDE.md"
    if claude_md.exists():
        shutil.copy(claude_md, worktree_path / "CLAUDE.md")

    # Record state
    st.upsert(
        repo_root, slug,
        prompt=prompt,
        worktree_path=str(worktree_path),
        branch=branch,
        session=session_name,
        status=st.PLANNING,
    )

    # Spin up tmux session
    loop = _loop_script()
    if not loop.exists():
        print(f"[ade] ERROR: loop script not found at {loop}", file=sys.stderr)
        sys.exit(1)

    try:
        sess.create(
            session_name=session_name,
            worktree_path=worktree_path,
            loop_script=loop,
            task_name=slug,
            prompt=prompt,
            repo_root=repo_root,
        )
    except RuntimeError as exc:
        print(f"[ade] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"[ade] Session '{session_name}' started.")
    print(f"[ade] Attach:  ade watch {task_name}")
    print(f"[ade] Status:  ade list")


def _cpu_for_pid(pid: int | None) -> str:
    if pid is None:
        return "    -"
    try:
        import psutil
        p = psutil.Process(pid)
        return f"{p.cpu_percent(interval=0.1):>4.1f}%"
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "%cpu="],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            return f"{r.stdout.strip():>5}"
    except Exception:
        pass
    return "    -"


def cmd_list() -> None:
    repo_root = _repo_root()
    all_state = st.load(repo_root)
    active_sessions = set(sess.list_sessions())

    if not all_state:
        print("[ade] No active tasks.")
        return

    header = f"{'TASK':<25}  {'STATUS':<12}  {'SESSION':<22}  {'CPU':>6}  {'RETRIES':>7}  PROMPT"
    print(header)
    print("-" * len(header))

    for entry in sorted(all_state.values(), key=lambda e: e.get("started_at", 0)):
        task = entry.get("task_name", "?")
        status = entry.get("status", "?")
        session_name = entry.get("session", "?")
        retries = entry.get("retries", 0)
        prompt = (entry.get("prompt", "") or "")[:40]
        alive = "●" if session_name in active_sessions else "○"
        pid = sess.session_pid(session_name) if session_name in active_sessions else None
        cpu = _cpu_for_pid(pid) if pid else "     -"
        print(f"{task:<25}  {status:<12}  {alive} {session_name:<20}  {cpu}  {retries:>7}  {prompt}")


def cmd_watch(task_name: str) -> None:
    slug = _slug(task_name)
    session_name = f"ade-{slug}"
    if not sess.exists(session_name):
        print(f"[ade] Session '{session_name}' not found.")
        sys.exit(1)
    sess.attach(session_name)


def cmd_sync(task_name: str) -> None:
    slug = _slug(task_name)
    repo_root = _repo_root()
    entry = st.get(repo_root, slug)

    if entry is None:
        print(f"[ade] No state found for task '{task_name}'.", file=sys.stderr)
        sys.exit(1)

    worktree_path = Path(entry["worktree_path"])
    branch = entry["branch"]

    # 1. Commit changes in the worktree
    print(f"[ade] Staging and committing worktree changes on {branch}…")
    _git_add_sync_changes(worktree_path)
    commit_result = subprocess.run(
        ["git", "commit", "-m", f"ade: {slug} — autonomous agent changes"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
    )
    if commit_result.returncode not in (0, 1):  # 1 = nothing to commit
        print(f"[ade] Commit: {commit_result.stdout.strip() or commit_result.stderr.strip()}")
    else:
        print(f"[ade] Committed.")

    # 2. Fetch latest main
    print("[ade] Fetching origin…")
    subprocess.run(
        ["git", "fetch", "origin"],
        cwd=str(repo_root),
        capture_output=True,
    )

    # 3. Merge the agent branch into main
    current_branch_r = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    current_branch = current_branch_r.stdout.strip()

    merge_result = subprocess.run(
        ["git", "merge", "--no-ff", branch, "-m", f"Merge ade/{slug} into {current_branch}"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )

    if merge_result.returncode == 0:
        print(f"[ade] Merged {branch} → {current_branch}.")
        st.set_status(repo_root, slug, st.DONE)
    else:
        print(f"[ade] Merge conflict — resolve manually then run 'git merge --continue'.")
        print(merge_result.stdout[-1000:])
        print(merge_result.stderr[-500:])
        notify.send(
            f"ADE: {task_name} — Merge conflict",
            "Resolve conflicts manually then 'git merge --continue'.",
        )


def cmd_approvals(approve_id: str | None = None, reject_id: str | None = None, reason: str = "") -> None:
    """
    List pending human-approval items, or approve/reject one by stream_id.

    ade approvals                   — list all pending
    ade approvals --approve <id>    — approve item
    ade approvals --reject  <id>    — reject item
    """
    import httpx as _httpx
    event_bus = os.getenv("EVENT_BUS_URL", "http://localhost:8766").rstrip("/")

    if approve_id or reject_id:
        stream_id = approve_id or reject_id
        decision = "approve" if approve_id else "reject"
        try:
            resp = _httpx.post(
                f"{event_bus}/approvals/{stream_id}",
                json={"decision": decision, "reason": reason},
                timeout=5.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                print(f"[ade] {decision.upper()}: task_id={data.get('task_id')}  stream_id={stream_id}")
            else:
                print(f"[ade] ERROR {resp.status_code}: {resp.text}", file=sys.stderr)
        except Exception as exc:
            print(f"[ade] Could not reach event bus: {exc}", file=sys.stderr)
        return

    # List pending
    try:
        resp = _httpx.get(f"{event_bus}/approvals/pending", timeout=5.0)
        items = resp.json()
    except Exception as exc:
        print(f"[ade] Could not reach event bus ({event_bus}): {exc}", file=sys.stderr)
        return

    if not items:
        print("[ade] No pending approvals.")
        return

    print(f"{'STREAM_ID':<20}  {'TASK_ID':<38}  OUTPUT EXCERPT")
    print("-" * 90)
    for item in items:
        sid = item.get("stream_id", "?")[:20]
        tid = item.get("task_id", "?")[:38]
        out = (item.get("output", "") or "")[:60].replace("\n", " ")
        print(f"{sid:<20}  {tid:<38}  {out}")
    print(f"\n{len(items)} pending.  Approve: ade approvals --approve <stream_id>")


def cmd_stop(task_name: str) -> None:
    slug = _slug(task_name)
    repo_root = _repo_root()
    session_name = f"ade-{slug}"
    worktree_path = repo_root / ".worktrees" / slug

    sess.kill(session_name)
    print(f"[ade] Killed session '{session_name}'.")

    if worktree_path.exists():
        r = subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            print(f"[ade] Removed worktree {worktree_path}.")
        else:
            print(f"[ade] Could not remove worktree: {r.stderr.strip()}")

    st.remove(repo_root, slug)
    print(f"[ade] State cleared for '{task_name}'.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="ade",
        description="Autonomous Dev Environment — run parallel Claude agents in isolated worktrees.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p_start = sub.add_parser("start", help="Start a new agent task")
    p_start.add_argument("task_name", help="Short name for the task (e.g. refactor-login)")
    p_start.add_argument("--prompt", default="", help="Task description (defaults to task_name)")

    p_list = sub.add_parser("list", help="Show all active agent sessions")

    p_watch = sub.add_parser("watch", help="Attach to a running session")
    p_watch.add_argument("task_name")

    p_sync = sub.add_parser("sync", help="Commit worktree changes and merge to main")
    p_sync.add_argument("task_name")

    p_stop = sub.add_parser("stop", help="Kill session and clean up worktree")
    p_stop.add_argument("task_name")

    p_approvals = sub.add_parser("approvals", help="List or act on pending human-approval items")
    p_approvals.add_argument("--approve", metavar="STREAM_ID", default=None)
    p_approvals.add_argument("--reject",  metavar="STREAM_ID", default=None)
    p_approvals.add_argument("--reason", default="")

    args = parser.parse_args(argv)

    if args.command == "start":
        prompt = args.prompt or args.task_name
        cmd_start(args.task_name, prompt)
    elif args.command == "list":
        cmd_list()
    elif args.command == "watch":
        cmd_watch(args.task_name)
    elif args.command == "sync":
        cmd_sync(args.task_name)
    elif args.command == "stop":
        cmd_stop(args.task_name)
    elif args.command == "approvals":
        cmd_approvals(
            approve_id=args.approve,
            reject_id=args.reject,
            reason=args.reason,
        )
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
