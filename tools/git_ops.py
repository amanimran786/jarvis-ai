"""
git_ops.py — Safe git operations for Jarvis tool routing.

Read ops (status, diff, log, branch, show) run without restriction.
Write ops (add, commit) require explicit params and path validation.
Push is intentionally excluded — it affects remote state and must be
confirmed by the user directly in the terminal.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TIMEOUT = 15


def _run(args: list[str], *, cwd: Path | None = None, check: bool = True) -> str:
    result = subprocess.run(
        args,
        cwd=str(cwd or _REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git exited {result.returncode}")
    return (result.stdout + result.stderr).strip()


def git_status() -> str:
    """Return `git status --short` for the repo root.

    --no-optional-locks keeps this a pure read: a plain `git status` can
    opportunistically rewrite the on-disk index, which briefly takes
    .git/index.lock, and a killed/timed-out process can leave that lock
    orphaned. This call only needs to read status.
    """
    try:
        out = _run(["git", "--no-optional-locks", "status", "--short"])
        return out if out else "Working tree is clean."
    except Exception as exc:
        log.warning("git status failed: %s", exc)
        return f"git status failed: {exc}"


def git_diff(staged: bool = False, path: str = "") -> str:
    """Return unified diff. staged=True shows --cached; path limits to one file."""
    try:
        args = ["git", "diff"]
        if staged:
            args.append("--cached")
        if path:
            safe = _safe_path(path)
            args += ["--", safe]
        out = _run(args, check=False)
        return out if out else "No diff."
    except Exception as exc:
        return f"git diff failed: {exc}"


def git_log(n: int = 10, oneline: bool = True) -> str:
    """Return last n commits. oneline=True for compact format."""
    try:
        args = ["git", "log", f"-{max(1, min(n, 50))}"]
        if oneline:
            args.append("--oneline")
        return _run(args)
    except Exception as exc:
        return f"git log failed: {exc}"


def git_branch() -> str:
    """List branches with current marked."""
    try:
        return _run(["git", "branch", "-v"])
    except Exception as exc:
        return f"git branch failed: {exc}"


def git_show(ref: str = "HEAD") -> str:
    """Show commit details for a ref (defaults to HEAD)."""
    try:
        safe_ref = ref.strip() or "HEAD"
        if not safe_ref.replace("-", "").replace(".", "").replace("_", "").replace("/", "").isalnum():
            return f"Invalid ref: {ref!r}"
        return _run(["git", "show", "--stat", safe_ref])
    except Exception as exc:
        return f"git show failed: {exc}"


def git_add(paths: list[str]) -> str:
    """Stage specific files. Refuses '.', '..', or globs."""
    if not paths:
        return "No paths provided."
    safe = []
    for p in paths:
        try:
            safe.append(_safe_path(p))
        except ValueError as exc:
            return f"Rejected: {exc}"
    try:
        _run(["git", "add", "--"] + safe)
        return f"Staged: {', '.join(safe)}"
    except Exception as exc:
        return f"git add failed: {exc}"


def git_add_all() -> str:
    """Stage all changes (respects .gitignore). Returns new status."""
    try:
        _run(["git", "add", "-A"])
        out = _run(["git", "status", "--short"])
        return out if out else "All changes staged (working tree now clean)."
    except Exception as exc:
        return f"git add -A failed: {exc}"


def git_commit(message: str) -> str:
    """Commit staged changes with message. Refuses empty or suspiciously short messages."""
    message = (message or "").strip()
    if len(message) < 5:
        return "Commit message too short (minimum 5 characters)."
    if any(c in message for c in ("`", "$", ";", "|", "&", ">")):
        return "Commit message contains disallowed shell characters."
    try:
        out = _run(["git", "commit", "-m", message])
        return out
    except Exception as exc:
        return f"git commit failed: {exc}"


# ── Path safety ───────────────────────────────────────────────────────────────

def _safe_path(user_input: str) -> str:
    """Resolve path and reject traversal outside repo root."""
    p = user_input.strip()
    if not p or p in (".", ".."):
        raise ValueError(f"Bare path not allowed: {p!r}")
    resolved = (_REPO_ROOT / p).resolve()
    if not str(resolved).startswith(str(_REPO_ROOT)):
        raise ValueError(f"Path traversal rejected: {p!r}")
    return str(resolved.relative_to(_REPO_ROOT))


# ── Unified dispatch ──────────────────────────────────────────────────────────

def dispatch(action: str, params: dict) -> tuple[bool, str]:
    """Route a git action from execution_engine. Returns (ok, output)."""
    action = (action or "").strip().lower()

    if action == "status":
        return True, git_status()

    if action == "diff":
        return True, git_diff(
            staged=str(params.get("staged", "false")).lower() in ("true", "1"),
            path=params.get("path", ""),
        )

    if action == "log":
        return True, git_log(
            n=int(params.get("n", 10)),
            oneline=str(params.get("oneline", "true")).lower() not in ("false", "0"),
        )

    if action == "branch":
        return True, git_branch()

    if action == "show":
        return True, git_show(params.get("ref", "HEAD"))

    if action == "add":
        raw = params.get("paths", params.get("path", ""))
        paths = [p.strip() for p in (raw if isinstance(raw, list) else raw.split(",")) if p.strip()]
        return True, git_add(paths)

    if action == "commit":
        return True, git_commit(params.get("message", ""))

    if action == "add_all":
        return True, git_add_all()

    return False, f"Unknown git action: {action!r}. Valid: status, diff, log, branch, show, add, add_all, commit."
