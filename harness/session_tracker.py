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
      "result_summary": null,             // agent report; never completion proof
      "attempt_id": "attempt_...",
      "contract_sha256": "...",
      "completion_commit": null,           // pinned clean HEAD on complete()
      "completion_evidence": null         // populated on complete()
    },
    ...
  ]
}

Public API:
    tracker = SessionTracker()
    tracker.claim(task_id, session_id)
    tracker.complete(session_id, result_summary, evidence={...})
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
from typing import Any, Iterable, Mapping

from harness.state_lock import state_file_lock

log = logging.getLogger(__name__)


class SessionTrackerError(RuntimeError):
    """Raised when durable session state cannot be read safely."""


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

    Mutations use a cross-process file lock and atomic replacement so concurrent
    launchers and orchestrator loops cannot overwrite each other's updates.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path: Path = path or (_base_dir() / "ACTIVE_SESSIONS.json")

    # ── public API ──────────────────────────────────────────────────────────

    def claim(
        self,
        task_id: str,
        session_id: str,
        *,
        attempt_id: str = "",
        contract_sha256: str = "",
        attempt_number: int = 1,
        repo_path: str = "",
        base_ref: str = "",
    ) -> None:
        """
        Register that *session_id* has picked up *task_id*.

        If session_id already has an active entry it is updated in place.
        """
        now = _now_iso()
        with state_file_lock(self._path):
            data = self._load()
            sessions: list[dict] = data["sessions"]

            existing = self._find(sessions, session_id)
            if existing is not None:
                existing["task_id"] = task_id
                existing["claimed_at"] = now
                existing["last_updated"] = now
                existing["status"] = "active"
                existing["result_summary"] = None
                existing["completion_evidence"] = None
                existing["completion_commit"] = None
                existing["attempt_id"] = attempt_id
                existing["contract_sha256"] = contract_sha256
                existing["attempt_number"] = attempt_number
                existing["repo_path"] = repo_path
                existing["base_ref"] = base_ref
            else:
                sessions.append({
                    "session_id": session_id,
                    "task_id": task_id,
                    "claimed_at": now,
                    "last_updated": now,
                    "status": "active",
                    "result_summary": None,
                    "completion_evidence": None,
                    "completion_commit": None,
                    "attempt_id": attempt_id,
                    "contract_sha256": contract_sha256,
                    "attempt_number": attempt_number,
                    "repo_path": repo_path,
                    "base_ref": base_ref,
                })

            self._save(data)
        log.info("[SessionTracker] %s claimed %s", session_id, task_id)

    def complete(
        self,
        session_id: str,
        result_summary: str,
        *,
        evidence: Mapping[str, Any] | None = None,
        completion_commit: str = "",
    ) -> bool:
        """
        Mark *session_id* as completed with the given result summary.

        The entry stays in the file (status="completed") so the orchestrator
        loop can harvest it and update WORK_QUEUE on the next iteration.
        """
        with state_file_lock(self._path):
            data = self._load()
            entry = self._find(data["sessions"], session_id)
            if entry is None:
                log.warning(
                    "[SessionTracker] complete() called for unknown session %s",
                    session_id,
                )
                return False

            entry["status"] = "completed"
            entry["last_updated"] = _now_iso()
            entry["result_summary"] = result_summary
            entry["completion_evidence"] = dict(evidence or {})
            entry["completion_commit"] = str(completion_commit or "").strip()
            self._save(data)
        log.info("[SessionTracker] %s completed", session_id)
        return True

    def fail(
        self,
        session_id: str,
        error: str,
        *,
        failure_class: str = "agent_error",
    ) -> None:
        """Record a terminal runtime failure for loop-owned retry routing."""
        with state_file_lock(self._path):
            data = self._load()
            entry = self._find(data["sessions"], session_id)
            if entry is None:
                log.warning(
                    "[SessionTracker] fail() called for unknown session %s",
                    session_id,
                )
                return
            entry["status"] = "completed"
            entry["last_updated"] = _now_iso()
            entry["result_summary"] = error
            entry["completion_evidence"] = None
            entry["completion_commit"] = None
            entry["runtime_failure_class"] = failure_class
            self._save(data)

    def remove(self, session_id: str) -> bool:
        """Remove one stale or superseded session identity."""
        with state_file_lock(self._path):
            data = self._load()
            before = len(data["sessions"])
            data["sessions"] = [
                session for session in data["sessions"]
                if session.get("session_id") != session_id
            ]
            if len(data["sessions"]) == before:
                return False
            self._save(data)
            return True

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
        with state_file_lock(self._path):
            data = self._load()
            entry = self._find(data["sessions"], session_id)
            if entry is not None:
                entry["last_updated"] = _now_iso()
                self._save(data)

    def expire_stalled(self, timeout_minutes: int = 90) -> list[dict[str, Any]]:
        """
        Mark stalled active sessions as 'stalled' and return the list.

        Sessions not updated within *timeout_minutes* are presumed dead —
        either no Cowork session ever opened them (LEGACY handoff-ready
        entries) or the agent crashed without calling complete().

        Caller should requeue their corresponding WORK_QUEUE tasks.
        """
        with state_file_lock(self._path):
            stalled = self.get_stalled(timeout_minutes=timeout_minutes)
            if not stalled:
                return []
            data = self._load()
            now = _now_iso()
            stalled_ids = {s["session_id"] for s in stalled}
            for session in data["sessions"]:
                if session.get("session_id") in stalled_ids:
                    session["status"] = "stalled"
                    session["last_updated"] = now
                    session["stall_reason"] = (
                        f"no activity for {timeout_minutes}+ minutes — auto-expired"
                    )
            self._save(data)
        log.info("[SessionTracker] expired %d stalled sessions", len(stalled))
        return stalled

    def purge_completed(self, session_ids: Iterable[str] | None = None) -> int:
        """Remove the harvested completed entries and return the count."""
        selected = {str(item) for item in session_ids} if session_ids is not None else None
        with state_file_lock(self._path):
            data = self._load()
            before = len(data["sessions"])
            data["sessions"] = [
                session
                for session in data["sessions"]
                if not (
                    session.get("status") == "completed"
                    and (
                        selected is None
                        or str(session.get("session_id") or "") in selected
                    )
                )
            ]
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
        """Read ACTIVE_SESSIONS.json and fail closed on corruption."""
        if not self._path.exists():
            return {"sessions": []}
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {"sessions": []}
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise SessionTrackerError(
                f"could not read session tracker: {self._path}"
            ) from exc
        if not isinstance(data, dict) or not isinstance(
            data.get("sessions"),
            list,
        ):
            raise SessionTrackerError(
                f"invalid session tracker structure: {self._path}"
            )
        return data

    def _save(self, data: dict[str, Any]) -> None:
        """Atomically write data to ACTIVE_SESSIONS.json."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(self._path) + ".tracker.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, str(self._path))
        except (OSError, TypeError, ValueError) as exc:
            log.exception("[SessionTracker] Failed to save %s", self._path)
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise OSError(f"could not persist session tracker: {self._path}") from exc

    @staticmethod
    def _find(sessions: list[dict], session_id: str) -> dict | None:
        return next((s for s in sessions if s.get("session_id") == session_id), None)
