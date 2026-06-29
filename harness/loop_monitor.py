"""
harness/loop_monitor.py — Status dashboard for the Jarvis autonomous loop.

Callable from the router (/status command) or directly from the terminal.

status_text() returns a formatted multi-line string covering:
  • Task counts in WORK_QUEUE.json broken down by status
  • Number of active sessions in ACTIVE_SESSIONS.json
  • Last 5 lines from MASTER_LOG.md
  • Any sessions stalled for >30 minutes (no last_updated update)

Public API:
    status_text(stall_minutes=30) -> str

CLI:
    python -m harness.loop_monitor
    python harness/loop_monitor.py
"""
from __future__ import annotations

import datetime
import json
import logging
import sys
from pathlib import Path
from typing import Any

# ── Bootstrap ─────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

log = logging.getLogger(__name__)

# ── File paths (overridable for tests) ────────────────────────────────────────
WORK_QUEUE_PATH      = _REPO_ROOT / "WORK_QUEUE.json"
ACTIVE_SESSIONS_PATH = _REPO_ROOT / "ACTIVE_SESSIONS.json"
MASTER_LOG_PATH      = _REPO_ROOT / "MASTER_LOG.md"
LAUNCH_QUEUE_PATH    = _REPO_ROOT / "LAUNCH_QUEUE.json"


# ── Public API ────────────────────────────────────────────────────────────────

def status_text(
    stall_minutes: int = 30,
    work_queue_path: Path | None = None,
    active_sessions_path: Path | None = None,
    master_log_path: Path | None = None,
    launch_queue_path: Path | None = None,
) -> str:
    """
    Return a formatted status dashboard string.

    Args:
        stall_minutes: sessions with no update beyond this threshold are flagged.
        *_path: override file paths (used in tests).

    Sections:
        1. Task queue summary (WORK_QUEUE.json counts by status)
        2. Active sessions (ACTIVE_SESSIONS.json)
        3. Launch queue pending count (LAUNCH_QUEUE.json)
        4. Last 5 log entries (MASTER_LOG.md)
        5. Stalled sessions alert
    """
    wq_path  = Path(work_queue_path)  if work_queue_path  else WORK_QUEUE_PATH
    as_path  = Path(active_sessions_path) if active_sessions_path else ACTIVE_SESSIONS_PATH
    ml_path  = Path(master_log_path)  if master_log_path  else MASTER_LOG_PATH
    lq_path  = Path(launch_queue_path) if launch_queue_path else LAUNCH_QUEUE_PATH

    lines: list[str] = []

    # ── 1. Task queue ─────────────────────────────────────────────────────────
    lines.append("## Jarvis Loop Status")
    lines.append("")
    lines.append("### Work Queue (WORK_QUEUE.json)")
    wq_counts = _work_queue_counts(wq_path)
    if wq_counts is None:
        lines.append("  (file not found)")
    else:
        total = sum(wq_counts.values())
        for status, count in sorted(wq_counts.items()):
            lines.append(f"  {status:12s}: {count:3d}")
        lines.append(f"  {'total':12s}: {total:3d}")

    # ── 2. Active sessions ────────────────────────────────────────────────────
    lines.append("")
    lines.append("### Active Sessions (ACTIVE_SESSIONS.json)")
    active_sessions = _load_active_sessions(as_path)
    if active_sessions is None:
        lines.append("  (file not found)")
    else:
        active = [s for s in active_sessions if s.get("status") == "active"]
        if not active:
            lines.append("  No active sessions.")
        else:
            for s in active:
                sid = s.get("session_id", "?")
                tid = s.get("task_id", "?")
                claimed = s.get("claimed_at", "?")[:16]   # trim to minute
                lines.append(f"  {sid}  →  {tid}  (since {claimed} UTC)")

    # ── 3. Launch queue pending ───────────────────────────────────────────────
    lines.append("")
    lines.append("### Launch Queue (LAUNCH_QUEUE.json)")
    lq_pending, lq_fired = _launch_queue_counts(lq_path)
    if lq_pending is None:
        lines.append("  (file not found)")
    else:
        lines.append(f"  pending : {lq_pending}")
        lines.append(f"  fired   : {lq_fired}")

    # ── 4. Recent log ─────────────────────────────────────────────────────────
    lines.append("")
    lines.append("### Recent Activity (last 5 log entries)")
    log_tail = _read_log_tail(ml_path, n=5)
    if log_tail is None:
        lines.append("  (MASTER_LOG.md not found)")
    elif not log_tail:
        lines.append("  (log is empty)")
    else:
        for entry in log_tail:
            lines.append(f"  {entry}")

    # ── 5. Stalled sessions ───────────────────────────────────────────────────
    stalled = _find_stalled(active_sessions or [], stall_minutes)
    lines.append("")
    if stalled:
        lines.append(f"### ⚠ Stalled Sessions (>{stall_minutes} min without update)")
        for s in stalled:
            sid      = s.get("session_id", "?")
            tid      = s.get("task_id", "?")
            last     = s.get("last_updated") or s.get("claimed_at") or "?"
            last_fmt = last[:16] if len(last) >= 16 else last
            lines.append(f"  STALLED: {sid}  (task {tid}, last update {last_fmt} UTC)")
    else:
        lines.append("### Stalled Sessions")
        lines.append(f"  None (threshold: {stall_minutes} min)")

    lines.append("")
    lines.append(f"_Generated {_now_fmt()} UTC_")

    return "\n".join(lines)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _work_queue_counts(path: Path) -> dict[str, int] | None:
    """Return {status: count} from WORK_QUEUE.json, or None if unreadable."""
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            queue = json.load(f)
        if not isinstance(queue, list):
            return {}
        counts: dict[str, int] = {}
        for task in queue:
            st = task.get("status", "unknown") if isinstance(task, dict) else "unknown"
            counts[st] = counts.get(st, 0) + 1
        return counts
    except Exception as exc:
        log.warning("[LoopMonitor] Could not read %s: %s", path, exc)
        return {}


def _load_active_sessions(path: Path) -> list[dict[str, Any]] | None:
    """Return sessions list from ACTIVE_SESSIONS.json, or None if unreadable."""
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        sessions = data.get("sessions", []) if isinstance(data, dict) else []
        return sessions if isinstance(sessions, list) else []
    except Exception as exc:
        log.warning("[LoopMonitor] Could not read %s: %s", path, exc)
        return []


def _launch_queue_counts(path: Path) -> tuple[int | None, int | None]:
    """Return (pending_count, fired_count) from LAUNCH_QUEUE.json."""
    if not path.exists():
        return None, None
    try:
        with open(path, encoding="utf-8") as f:
            queue = json.load(f)
        if not isinstance(queue, list):
            return 0, 0
        pending = sum(1 for e in queue if isinstance(e, dict) and e.get("status") == "pending")
        fired   = sum(1 for e in queue if isinstance(e, dict) and e.get("status") == "fired")
        return pending, fired
    except Exception as exc:
        log.warning("[LoopMonitor] Could not read %s: %s", path, exc)
        return 0, 0


def _read_log_tail(path: Path, n: int = 5) -> list[str] | None:
    """Return the last *n* non-empty lines from MASTER_LOG.md, or None if missing."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        lines = [l.rstrip() for l in text.splitlines() if l.strip()]
        return lines[-n:] if lines else []
    except Exception as exc:
        log.warning("[LoopMonitor] Could not read %s: %s", path, exc)
        return []


def _find_stalled(sessions: list[dict[str, Any]], timeout_minutes: int) -> list[dict[str, Any]]:
    """Return active sessions whose last_updated is older than timeout_minutes."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=timeout_minutes)
    stalled: list[dict] = []
    for s in sessions:
        if s.get("status") != "active":
            continue
        last = s.get("last_updated") or s.get("claimed_at")
        if not last:
            stalled.append(s)
            continue
        try:
            last_dt = datetime.datetime.fromisoformat(last)
            # Ensure timezone-aware comparison
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=datetime.timezone.utc)
            if last_dt < cutoff:
                stalled.append(s)
        except ValueError:
            stalled.append(s)   # unparseable → treat as stalled
    return stalled


def _now_fmt() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")


# ── CLI entry ─────────────────────────────────────────────────────────────────

def _cli() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Jarvis loop status dashboard")
    p.add_argument("--stall-minutes", type=int, default=30,
                   help="Minutes without update before a session is flagged as stalled (default 30)")
    args = p.parse_args()
    print(status_text(stall_minutes=args.stall_minutes))


if __name__ == "__main__":
    _cli()
