"""
harness/loop_watchdog.py — Resume signal check and heartbeat for the self-eval loop.

Call check_resume_signal() at the TOP of each loop iteration.
Call heartbeat(current_task) at the END of each loop iteration.

CLI usage (called by the loop engineer between cycles):
    python -m harness.loop_watchdog --check          # check + consume resume signal
    python -m harness.loop_watchdog --beat "task"    # write heartbeat
    python -m harness.loop_watchdog --check --beat "task"  # both
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_BASE = Path(__file__).resolve().parent.parent
_SIGNAL_PATH = _BASE / "RESUME_SIGNAL.json"
_STATUS_PATH = _BASE / "ORCHESTRATOR_STATUS.json"
_SESSION_KEY = "jarvis-self-eval"


# ── Resume signal ─────────────────────────────────────────────────────────────

def check_resume_signal() -> str | None:
    """Check for a resume signal. Consumes it (deletes the file) if found.

    Returns the reason string if a signal was present, else None.
    Prints the watchdog message per the spec.
    """
    if not _SIGNAL_PATH.exists():
        return None
    try:
        with open(_SIGNAL_PATH) as f:
            sig = json.load(f)
        if sig.get("signal") == "resume":
            reason = sig.get("reason", "(no reason given)")
            print(f"[WATCHDOG] Resume signal received: {reason}. Continuing loop.")
            _SIGNAL_PATH.unlink(missing_ok=True)
            return reason
        # File exists but signal type is unknown — remove anyway to avoid stale state
        _SIGNAL_PATH.unlink(missing_ok=True)
    except Exception:
        log.debug("[loop_watchdog] check_resume_signal() failed", exc_info=True)
    return None


# ── Heartbeat ─────────────────────────────────────────────────────────────────

def heartbeat(current_task: str, status: str = "active") -> None:
    """Write a heartbeat entry to ORCHESTRATOR_STATUS.json.

    Args:
        current_task: Short description of what was just built / is running.
        status: "active" (default) | "idle" | "done"
    """
    try:
        if _STATUS_PATH.exists():
            with open(_STATUS_PATH) as f:
                data = json.load(f)
        else:
            data = {}

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        entry = {
            "session_id": _SESSION_KEY,
            "name": _SESSION_KEY,
            "last_active": now,
            "current_task": current_task,
            "status": status,
        }

        # ORCHESTRATOR_STATUS.json uses sessions as a list of objects (keyed by session_id)
        sessions = data.get("sessions", [])
        if isinstance(sessions, list):
            existing = next((s for s in sessions if s.get("session_id") == _SESSION_KEY), None)
            if existing is not None:
                existing.update(entry)
            else:
                sessions.append(entry)
            data["sessions"] = sessions
        elif isinstance(sessions, dict):
            # Legacy dict format — update in place
            sessions[_SESSION_KEY] = entry
            data["sessions"] = sessions
        else:
            data["sessions"] = [entry]

        tmp = str(_STATUS_PATH) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _STATUS_PATH)
    except Exception:
        log.debug("[loop_watchdog] heartbeat() failed", exc_info=True)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="harness.loop_watchdog")
    parser.add_argument("--check", action="store_true", help="Check and consume resume signal")
    parser.add_argument("--beat", metavar="TASK", help="Write heartbeat with this task description")
    parser.add_argument("--status", default="active", help="Status string for heartbeat (default: active)")
    args = parser.parse_args(argv)

    if not args.check and not args.beat:
        parser.print_help()
        return 0

    if args.check:
        reason = check_resume_signal()
        if reason is None:
            print("[WATCHDOG] No resume signal.")

    if args.beat:
        heartbeat(args.beat, status=args.status)
        print(f"[WATCHDOG] Heartbeat written: {args.beat!r} ({args.status})")

    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
