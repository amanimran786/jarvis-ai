"""
harness/loop.py — Loop control and heartbeat for the Jarvis engineering loop.

Provides two primitives the loop runner calls on every iteration:

  check_resume_signal()  — read RESUME_SIGNAL.json; if signal=="resume", log
                           the reason and delete the file so the loop continues.
                           Callers can inspect the returned dict to adjust behavior.

  heartbeat(task)        — write the current task + timestamp to
                           ORCHESTRATOR_STATUS.json under sessions["jarvis-local-llm"].
                           Called at the END of each loop iteration.

These are stdlib-only (json, os, datetime) so they can be imported from anywhere
without circular-import risk.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def _base_dir() -> Path:
    try:
        import sys
        if getattr(sys, "frozen", False):
            import runtime_state  # type: ignore
            return runtime_state.app_data_dir()
    except Exception:
        logging.debug("[Loop] silent failure in _base_dir", exc_info=True)
    return Path(__file__).resolve().parent.parent


def _signal_path() -> Path:
    return _base_dir() / "RESUME_SIGNAL.json"


def _status_path() -> Path:
    return _base_dir() / "ORCHESTRATOR_STATUS.json"


# ── Resume signal ─────────────────────────────────────────────────────────────

def check_resume_signal() -> dict:
    """
    Read RESUME_SIGNAL.json if it exists.

    If signal == "resume": log the reason, delete the file, return the signal dict.
    If the file doesn't exist or is malformed: return {}.

    Callers use the returned dict to decide whether to skip straight to a specific
    task or just continue normally.

    Example RESUME_SIGNAL.json:
        {"signal": "resume", "reason": "rate limit reset", "task": "harness/budget.py"}
    """
    path = _signal_path()
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            sig = json.load(f)
    except Exception as exc:
        log.debug("[WatchDog] Could not read %s: %s", path, exc)
        return {}

    if sig.get("signal") == "resume":
        reason = sig.get("reason", "(no reason given)")
        task   = sig.get("task", "")
        log.info("[WatchDog] Resume signal received: %s%s. Continuing loop.",
                 reason, f" — task: {task}" if task else "")
        print(f"[WATCHDOG] Resume signal received: {reason}. Continuing loop.")
        try:
            os.remove(path)
        except OSError as exc:
            log.debug("[WatchDog] Could not delete signal file: %s", exc)

    return sig


# ── Heartbeat ─────────────────────────────────────────────────────────────────

_SESSION_KEY = "jarvis-local-llm"


def heartbeat(task: str, *, status: str = "active", extra: dict | None = None) -> None:
    """
    Write current loop state to ORCHESTRATOR_STATUS.json.

    Called at the END of each loop iteration so external monitors can see
    progress without polling logs.

    Args:
        task   — short description of what was just built / is in progress
        status — "active" (default) | "idle" | "blocked"
        extra  — optional dict merged into the session entry
    """
    path = _status_path()
    now  = datetime.datetime.now(datetime.timezone.utc).isoformat()

    try:
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}

        sessions = data.get("sessions")
        # Support both dict (old) and list (new harness/audit.py) formats
        if isinstance(sessions, list):
            entry = next((s for s in sessions if s.get("session_id") == _SESSION_KEY), None)
            if entry is None:
                entry = {"session_id": _SESSION_KEY, "name": _SESSION_KEY, "completed_tasks": []}
                sessions.append(entry)
        else:
            if sessions is None:
                data["sessions"] = {}
            entry = data["sessions"].setdefault(_SESSION_KEY, {})

        entry["last_active"]   = now
        entry["current_task"]  = task
        entry["status"]        = status
        if extra:
            entry.update(extra)

        tmp = str(path) + ".loop.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, str(path))

    except Exception as exc:
        log.debug("[WatchDog] heartbeat write failed: %s", exc)
