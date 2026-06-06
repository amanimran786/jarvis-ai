"""
ade/loop.py — Plan → Execute → Verify → Retry agent loop.

Runs inside a tmux pane. Invoked by scripts/ade-loop.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from ade import notify, state as st

MAX_RETRIES = 3
CLAUDE_BIN = "claude"
TIMEOUT_PLAN_SEC = 300
TIMEOUT_EXEC_SEC = 1800  # 30 min hard cap per phase
TIMEOUT_TEST_SEC = 600


# ── Test detection ────────────────────────────────────────────────────────────

def detect_test_cmd(root: Path) -> list[str] | None:
    """Return the test command for the project at root, or None if undetectable."""
    if (root / "package.json").exists():
        import json
        try:
            pkg = json.loads((root / "package.json").read_text())
            if "test" in pkg.get("scripts", {}):
                return ["npm", "test"]
        except Exception:
            pass

    if (root / "Makefile").exists():
        content = (root / "Makefile").read_text()
        if "\ntest:" in content or content.startswith("test:"):
            return ["make", "test"]

    if (
        (root / "pytest.ini").exists()
        or (root / "pyproject.toml").exists()
        or (root / "setup.cfg").exists()
        or (root / "tests").is_dir()
    ):
        return [sys.executable, "-m", "pytest", "-q", "--tb=short"]

    if (root / "Cargo.toml").exists():
        return ["cargo", "test"]

    return None


# ── Subprocess helpers ────────────────────────────────────────────────────────

def _run(cmd: list[str], cwd: Path, timeout: int, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input_text,
    )


def _run_interactive(cmd: list[str], cwd: Path) -> int:
    """Run a command with full stdin/stdout (for interactive claude sessions)."""
    proc = subprocess.run(cmd, cwd=str(cwd))
    return proc.returncode


def _claude_prompt_cmd(prompt: str) -> list[str]:
    cmd = [CLAUDE_BIN, "-p", prompt]
    if os.getenv("ADE_CLAUDE_SKIP_PERMISSIONS", "").lower() in {"1", "true", "yes"}:
        cmd.insert(1, "--dangerously-skip-permissions")
    return cmd


# ── Loop phases ───────────────────────────────────────────────────────────────

def phase_plan(task_name: str, prompt: str, worktree: Path, repo_root: Path) -> bool:
    """
    Ask claude to write PLAN.md only.
    Returns True when PLAN.md exists and user approves.
    """
    st.set_status(repo_root, task_name, st.PLANNING)
    print(f"\n{'='*60}")
    print(f"  ADE  [{task_name}]  PHASE 1 — PLANNING")
    print(f"{'='*60}\n")

    plan_prompt = (
        f"Task: {prompt}\n\n"
        "Before writing any code, create a file called PLAN.md in the current directory. "
        "The plan must list: (1) which files you will create or modify, (2) step-by-step "
        "implementation order, (3) which tests you will write or update, (4) any risks or "
        "assumptions. Write ONLY PLAN.md at this stage — no source code changes yet."
    )

    plan_file = worktree / "PLAN.md"
    plan_file.unlink(missing_ok=True)

    rc = _run_interactive(_claude_prompt_cmd(plan_prompt), worktree)

    if not plan_file.exists():
        print("\n[ADE] claude did not write PLAN.md — skipping plan review.")
    else:
        print(f"\n[ADE] PLAN.md written ({plan_file.stat().st_size} bytes).")
        print("\n--- PLAN START ---")
        print(plan_file.read_text())
        print("--- PLAN END ---\n")

    notify.send(
        f"ADE: {task_name} — Plan ready",
        "Review PLAN.md, then press Enter in the session to approve.",
    )

    print("[ADE] Press Enter to approve and start execution, or Ctrl-C to abort: ", end="", flush=True)
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        print("\n[ADE] Aborted by user.")
        st.set_status(repo_root, task_name, st.FAILED, retries=0)
        return False

    return True


def phase_execute(task_name: str, prompt: str, worktree: Path, repo_root: Path, error_context: str = "") -> int:
    """
    Run claude to implement the task. Returns exit code.
    """
    st.set_status(repo_root, task_name, st.EXECUTING)
    print(f"\n{'='*60}")
    print(f"  ADE  [{task_name}]  PHASE 2 — EXECUTING")
    print(f"{'='*60}\n")

    exec_prompt = f"Implement the plan from PLAN.md for this task: {prompt}"
    if error_context:
        exec_prompt = (
            f"The previous attempt failed tests. Fix ALL failures.\n\n"
            f"Test errors:\n{error_context}\n\n"
            f"Original task: {prompt}\n"
            f"Refer to PLAN.md for context."
        )

    return _run_interactive(_claude_prompt_cmd(exec_prompt), worktree)


def phase_verify(task_name: str, worktree: Path, repo_root: Path) -> tuple[bool, str]:
    """
    Run the test suite. Returns (passed, error_output).
    """
    st.set_status(repo_root, task_name, st.VERIFYING)
    print(f"\n{'='*60}")
    print(f"  ADE  [{task_name}]  PHASE 3 — VERIFYING")
    print(f"{'='*60}\n")

    test_cmd = detect_test_cmd(worktree)
    if test_cmd is None:
        print("[ADE] No test suite detected — skipping verification.")
        return True, ""

    print(f"[ADE] Running: {' '.join(test_cmd)}")
    try:
        result = _run(test_cmd, worktree, TIMEOUT_TEST_SEC)
        output = (result.stdout + result.stderr).strip()
        if result.returncode == 0:
            print("[ADE] Tests PASSED.")
            return True, ""
        print(f"[ADE] Tests FAILED (exit {result.returncode}):\n{output[-3000:]}")
        return False, output[-3000:]
    except subprocess.TimeoutExpired:
        msg = f"test suite timed out after {TIMEOUT_TEST_SEC}s"
        print(f"[ADE] {msg}")
        return False, msg


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(task_name: str, prompt: str, worktree: Path, repo_root: Path) -> None:
    approved = phase_plan(task_name, prompt, worktree, repo_root)
    if not approved:
        return

    error_ctx = ""
    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            st.set_status(repo_root, task_name, st.RETRYING, retries=attempt)
            print(f"\n[ADE] Retry {attempt}/{MAX_RETRIES}…")
            notify.send(
                f"ADE: {task_name} — Retrying ({attempt}/{MAX_RETRIES})",
                "Tests failed; re-running with error context.",
            )

        phase_execute(task_name, prompt, worktree, repo_root, error_context=error_ctx)
        passed, error_ctx = phase_verify(task_name, worktree, repo_root)

        if passed:
            st.set_status(repo_root, task_name, st.DONE, retries=attempt)
            print(f"\n[ADE] ✓ {task_name} completed successfully.")
            notify.send(
                f"ADE: {task_name} — Done",
                "All tests passed. Run 'ade sync' to merge.",
            )
            return

    # All retries exhausted
    st.set_status(repo_root, task_name, st.FAILED, retries=MAX_RETRIES)
    print(f"\n[ADE] ✗ {task_name} FAILED after {MAX_RETRIES} retries.")
    notify.send(
        f"ADE: {task_name} — FAILED",
        f"All {MAX_RETRIES} retries exhausted. Manual intervention needed.",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="ADE agent loop — runs inside a tmux session.")
    parser.add_argument("--task", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--worktree", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    args = parser.parse_args()

    run(
        task_name=args.task,
        prompt=args.prompt,
        worktree=args.worktree,
        repo_root=args.repo_root,
    )


if __name__ == "__main__":
    main()
