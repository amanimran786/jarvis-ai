#!/usr/bin/env python3
"""orchestrate.py — the conductor layer over ADE.

This is the command surface a coordinating Claude session (or a human) uses to
run parallel autonomous agents as ADE worktree+tmux lanes. It wraps `ade` with
orchestrator-safe defaults so an unattended dispatch is a single command that
can't accidentally hang or run the full repo suite:

  ADE_AUTO_APPROVE_PLAN=1        always — no human plan gate in a detached pane
  ADE_CLAUDE_SKIP_PERMISSIONS=1  default on — headless agents can act (--supervised to opt out)
  ADE_TEST_CMD=<scoped>          when --tests given — per-lane tests, not full suite
  ADE_SKIP_VERIFY=1              when --skip-verify given — no-code lanes

Commands:
  orchestrate dispatch <lane> --prompt TEXT [--tests CMD] [--skip-verify] [--supervised]
  orchestrate status
  orchestrate harvest <lane> [--yes]
  orchestrate abort <lane>

Lanes are git worktrees under .worktrees/<lane> on branch ade/<lane>. main is
only touched by `harvest` (which runs `ade sync`), never by a running agent.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent
_ADE = [sys.executable, str(_REPO_ROOT / "ade_cmd.py")]

sys.path.insert(0, str(_REPO_ROOT))


# ── Dispatch env (safety-critical) ────────────────────────────────────────────

def build_dispatch_env(
    base_env: dict[str, str],
    *,
    tests: str | None,
    skip_verify: bool,
    supervised: bool,
) -> dict[str, str]:
    """Construct the environment for an unattended ADE lane.

    Always auto-approves the plan gate. Skip-permissions is on unless supervised.
    Scopes tests when given; skips verification when asked. Leaving both test
    options unset means the lane falls back to the full repo suite — the caller
    is responsible for that choice (status/dispatch warns).
    """
    env = dict(base_env)
    env["ADE_AUTO_APPROVE_PLAN"] = "1"
    if supervised:
        env.pop("ADE_CLAUDE_SKIP_PERMISSIONS", None)
    else:
        env["ADE_CLAUDE_SKIP_PERMISSIONS"] = "1"

    # Test scoping is mutually exclusive: skip wins, then explicit cmd, else none.
    env.pop("ADE_TEST_CMD", None)
    env.pop("ADE_SKIP_VERIFY", None)
    if skip_verify:
        env["ADE_SKIP_VERIFY"] = "1"
    elif tests:
        env["ADE_TEST_CMD"] = tests
    return env


def dispatch(lane: str, prompt: str, *, tests: str | None, skip_verify: bool, supervised: bool) -> int:
    env = build_dispatch_env(dict(os.environ), tests=tests, skip_verify=skip_verify, supervised=supervised)

    if not skip_verify and not tests:
        print("[orchestrate] ⚠ no --tests / --skip-verify: this lane will run the FULL repo suite "
              "(slow, may trip unrelated flakes). Scope it unless you mean it.")
    mode = "supervised" if supervised else "autonomous (skip-permissions)"
    scope = "skip-verify" if skip_verify else (f"tests={tests!r}" if tests else "FULL SUITE")
    print(f"[orchestrate] dispatch lane={lane!r}  mode={mode}  {scope}")

    r = subprocess.run([*_ADE, "start", lane, "--prompt", prompt], env=env)
    return r.returncode


# ── Status roll-up (pure reads, mockable) ─────────────────────────────────────

def _load_state() -> dict[str, Any]:
    from ade import state as st
    return st.load(_REPO_ROOT)


def _live_sessions() -> set[str]:
    from ade import session as sess
    return set(sess.list_sessions())


def _trunk_ref() -> str:
    """The integration branch a lane is harvested into (override with ADE_TRUNK)."""
    return os.getenv("ADE_TRUNK", "main").strip() or "main"


def _merge_base(worktree: Path, ref: str) -> str | None:
    """Fork point of the lane's HEAD and ref, or None if ref is unresolvable here."""
    try:
        r = subprocess.run(
            ["git", "merge-base", ref, "HEAD"],
            cwd=str(worktree), capture_output=True, text=True, timeout=30, shell=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    base = r.stdout.strip()
    return base if (r.returncode == 0 and base) else None


def _diff_stat(worktree: Path) -> tuple[int, int]:
    """Return (files_changed, lines_changed) for ALL work the lane produced.

    Measured from the merge-base with the trunk branch, NOT vs HEAD. ADE agents
    commit their output to the lane branch, so a diff against HEAD reports zero
    for a successful, committed lane — making real work look empty and tripping a
    human conductor into discarding it. The merge-base baseline captures both the
    commits the lane added on top of trunk and any uncommitted working-tree edits,
    and is robust to trunk advancing after the lane forked. Untracked files
    (e.g. PLAN.md) are still counted. Falls back to HEAD when trunk is
    unresolvable (non-main repo, detached), preserving the old behavior.
    """
    if not worktree.exists():
        return (0, 0)
    base = _merge_base(worktree, _trunk_ref()) or "HEAD"
    try:
        tracked = subprocess.run(
            ["git", "diff", "--numstat", base],
            cwd=str(worktree), capture_output=True, text=True, timeout=30, shell=False,
        )
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(worktree), capture_output=True, text=True, timeout=30, shell=False,
        )
    except (subprocess.SubprocessError, OSError):
        return (0, 0)
    files = 0
    lines = 0
    for ln in tracked.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) == 3:
            files += 1
            for n in parts[:2]:
                if n.isdigit():
                    lines += int(n)
    untracked_files = [u for u in untracked.stdout.splitlines() if u.strip()]
    files += len(untracked_files)
    return (files, lines)


def collect_status() -> list[dict[str, Any]]:
    state = _load_state()
    live = _live_sessions()
    rows: list[dict[str, Any]] = []
    for entry in sorted(state.values(), key=lambda e: e.get("started_at", 0)):
        lane = entry.get("task_name", "?")
        status = entry.get("status", "?")
        session = entry.get("session", "")
        worktree = Path(entry.get("worktree_path", "")) if entry.get("worktree_path") else _REPO_ROOT / ".worktrees" / lane
        files, lines = _diff_stat(worktree)
        rows.append({
            "lane": lane,
            "status": status,
            "retries": entry.get("retries", 0),
            "alive": session in live,
            "files": files,
            "lines": lines,
            "ready_to_sync": status == "DONE" and files > 0,
            "prompt": (entry.get("prompt", "") or "")[:48],
        })
    return rows


def render_status(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "[orchestrate] No lanes. Dispatch one: orchestrate dispatch <lane> --prompt '…' --tests '…'"
    out = [f"{'LANE':<22} {'STATUS':<11} {'ALIVE':<6} {'RET':<4} {'DIFF':<12} {'SYNC':<5} PROMPT",
           "-" * 92]
    for r in rows:
        alive = "●" if r["alive"] else "○"
        diff = f"{r['files']}f/{r['lines']}L" if r["files"] else "—"
        sync = "▲" if r["ready_to_sync"] else ""
        out.append(f"{r['lane']:<22} {r['status']:<11} {alive:<6} {r['retries']:<4} {diff:<12} {sync:<5} {r['prompt']}")
    ready = [r["lane"] for r in rows if r["ready_to_sync"]]
    if ready:
        out.append("")
        out.append(f"Ready to harvest: {', '.join(ready)}   →  orchestrate harvest <lane>")
    return "\n".join(out)


# ── Harvest / abort ───────────────────────────────────────────────────────────

def harvest(lane: str, assume_yes: bool) -> int:
    worktree = _REPO_ROOT / ".worktrees" / lane
    state = _load_state()
    entry = state.get(lane)
    if entry and entry.get("worktree_path"):
        worktree = Path(entry["worktree_path"])

    print(f"[orchestrate] Diff for lane {lane!r} (worktree {worktree}):\n")
    subprocess.run(["git", "diff", "--stat", "HEAD"], cwd=str(worktree), shell=False)
    if not assume_yes:
        print(f"\n[orchestrate] Review above. To merge into main run: orchestrate harvest {lane} --yes")
        return 0
    print(f"\n[orchestrate] Merging lane {lane!r} → main via `ade sync`…")
    r = subprocess.run([*_ADE, "sync", lane], shell=False)
    return r.returncode


def abort(lane: str) -> int:
    r = subprocess.run([*_ADE, "stop", lane], shell=False)
    return r.returncode


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="orchestrate", description="Conductor over ADE autonomous lanes.")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    d = sub.add_parser("dispatch", help="Spawn an autonomous agent lane")
    d.add_argument("lane")
    d.add_argument("--prompt", required=True)
    d.add_argument("--tests", default=None, help="Scoped test command for this lane, e.g. 'pytest tests/test_x.py -q'")
    d.add_argument("--skip-verify", action="store_true", help="No-code lane: skip the test phase entirely")
    d.add_argument("--supervised", action="store_true", help="Do NOT pass --dangerously-skip-permissions")

    sub.add_parser("status", help="Show all lanes, liveness, diff size, sync-readiness")

    h = sub.add_parser("harvest", help="Show a lane's diff; with --yes, merge to main")
    h.add_argument("lane")
    h.add_argument("--yes", action="store_true", help="Actually merge (ade sync)")

    a = sub.add_parser("abort", help="Kill a lane and remove its worktree")
    a.add_argument("lane")

    args = p.parse_args(argv)

    if args.command == "dispatch":
        sys.exit(dispatch(args.lane, args.prompt, tests=args.tests,
                          skip_verify=args.skip_verify, supervised=args.supervised))
    elif args.command == "status":
        print(render_status(collect_status()))
    elif args.command == "harvest":
        sys.exit(harvest(args.lane, args.yes))
    elif args.command == "abort":
        sys.exit(abort(args.lane))
    else:
        p.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
