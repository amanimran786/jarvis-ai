"""
harness/session_tracker.py — Track active AI sessions and their assigned tasks.

Backed by ACTIVE_SESSIONS.json at the repo root.  All mutations are atomic
(write-to-tmp then os.replace) so concurrent readers never see a partial write.

Format of ACTIVE_SESSIONS.json:
{
  "sessions": [
    {
      "session_id":    "jarvis-board",
      "task_id":       "TASK-042",
      "claimed_at":    "2026-06-28T01:00:00+00:00",
      "last_updated":  "2026-06-28T01:05:00+00:00",
      "status":        "active",          // "active" | "completed"
      "result_summary": null              // populated on complete()
    },
    ...
  ]
}

Public API:
    tracker = SessionTracker()
    tracker.claim(task_id, session_id)
    tracker.complete(session_id, result_summary)
    tracker.active_count() -> int
    tracker.get_stalled(timeout_minutes=30) -> list[dict]
    tracker.list_active() -> list[dict]
"""
from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _base_dir() -> Path:
    """Project root — same logic as loop.py and audit.py."""
    try:
        import sys
        if getattr(sys, "frozen", False):
            import runtime_state  # type: ignore[import]
            return runtime_state.app_data_dir()
    except Exception:
        pass
    return Path(__file__).resolve().parent.parent


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ── SessionTracker ─────────────────────────────────────────────────────────────

class SessionTracker:
    """
    Persistent, file-backed tracker for active Jarvis sessions.

    Thread-safety: file writes are atomic (tmp-rename), but the class itself
    does not hold a lock — callers in the same process that write concurrently
    may race.  For the orchestrator's single-process loop this is fine.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path: Path = path or (_base_dir() / "ACTIVE_SESSIONS.json")

    # ── public API ──────────────────────────────────────────────────────────

    def claim(self, task_id: str, session_id: str) -> None:
        """
        Register that *session_id* has picked up *task_id*.

        If session_id already has an active entry it is updated in place.
        """
        now = _now_iso()
        data = self._load()
        sessions: list[dict] = data["sessions"]

        existing = self._find(sessions, session_id)
        if existing is not None:
            existing["task_id"]      = task_id
            existing["claimed_at"]   = now
            existing["last_updated"] = now
            existing["status"]       = "active"
            existing["result_summary"] = None
        else:
            sessions.append({
                "session_id":     session_id,
                "task_id":        task_id,
                "claimed_at":     now,
                "last_updated":   now,
                "status":         "active",
                "result_summary": None,
            })

        self._save(data)
        log.info("[SessionTracker] %s claimed %s", session_id, task_id)

    def complete(self, session_id: str, result_summary: str) -> None:
        """
        Mark *session_id* as completed with the given result summary.

        The entry stays in the file (status="completed") so the orchestrator
        loop can harvest it and update WORK_QUEUE on the next iteration.
        """
        data = self._load()
        entry = self._find(data["sessions"], session_id)
        if entry is None:
            log.warning("[SessionTracker] complete() called for unknown session %s", session_id)
            return

        entry["status"]         = "completed"
        entry["last_updated"]   = _now_iso()
        entry["result_summary"] = result_summary
        self._save(data)
        log.info("[SessionTracker] %s completed", session_id)

    def active_count(self) -> int:
        """Return the number of sessions currently in status 'active'."""
        return len([s for s in self._load()["sessions"] if s.get("status") == "active"])

    def get_stalled(self, timeout_minutes: int = 30) -> list[dict[str, Any]]:
        """
        Return active sessions whose last_updated is older than *timeout_minutes*.

        A stalled session might have crashed without calling complete().
        """
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            minutes=timeout_minutes
        )
        stalled = []
        for s in self._load()["sessions"]:
            if s.get("status") != "active":
                continue
            last = s.get("last_updated") or s.get("claimed_at")
            if not last:
                stalled.append(s)
                continue
            try:
                last_dt = datetime.datetime.fromisoformat(last)
                if last_dt < cutoff:
                    stalled.append(s)
            except ValueError:
                stalled.append(s)   # unparseable timestamp → treat as stalled
        return stalled

    def list_active(self) -> list[dict[str, Any]]:
        """Return all sessions with status 'active'."""
        return [s for s in self._load()["sessions"] if s.get("status") == "active"]

    def touch(self, session_id: str) -> None:
        """
        Update last_updated for *session_id* to now.

        Called by long-running sessions periodically to prevent stall detection.
        """
        data = self._load()
        entry = self._find(data["sessions"], session_id)
        if entry is not None:
            entry["last_updated"] = _now_iso()
            self._save(data)

    def purge_completed(self) -> int:
        """Remove all completed entries. Returns count removed."""
        data = self._load()
        before = len(data["sessions"])
        data["sessions"] = [s for s in data["sessions"] if s.get("status") != "completed"]
        removed = before - len(data["sessions"])
        if removed:
            self._save(data)
            log.debug("[SessionTracker] purged %d completed entries", removed)
        return removed

    def list_completed(self) -> list[dict[str, Any]]:
        """Return sessions with status='completed' (pending harvest)."""
        return [s for s in self._load()["sessions"] if s.get("status") == "completed"]

    # ── internals ────────────────────────────────────────────────────────────

    def _load(self) -> dict[str, Any]:
        """Read ACTIVE_SESSIONS.json; return empty structure if missing/corrupt."""
        if not self._path.exists():
            return {"sessions": []}
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            if "sessions" not in data or not isinstance(data["sessions"], list):
                data["sessions"] = []
            return data
        except Exception as exc:
            log.warning("[SessionTracker] Could not read %s: %s — starting fresh", self._path, exc)
            return {"sessions": []}

    def _save(self, data: dict[str, Any]) -> None:
        """Atomically write data to ACTIVE_SESSIONS.json."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(self._path) + ".tracker.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, str(self._path))
        except Exception as exc:
            log.error("[SessionTracker] Failed to save %s: %s", self._path, exc)
            try:
                os.remove(tmp)
            except OSError:
                pass

    @staticmethod
    def _find(sessions: list[dict], session_id: str) -> dict | None:
        return next((s for s in sessions if s.get("session_id") == session_id), None)
