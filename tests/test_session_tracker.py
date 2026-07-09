"""Tests for harness/session_tracker.py — focused on expire_stalled()."""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.session_tracker import SessionTracker


def _make_tracker(tmp_path: Path) -> SessionTracker:
    return SessionTracker(path=tmp_path / "ACTIVE_SESSIONS.json")


def _iso(minutes_ago: int = 0) -> str:
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
    return dt.isoformat()


def _write_sessions(tracker: SessionTracker, sessions: list[dict]) -> None:
    tracker._save({"sessions": sessions})


# ── expire_stalled ─────────────────────────────────────────────────────────────

class TestExpireStalled:
    def test_no_sessions_returns_empty(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        assert tracker.expire_stalled(timeout_minutes=30) == []

    def test_fresh_active_session_not_expired(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        _write_sessions(tracker, [{
            "session_id": "s1",
            "task_id": "TASK-001",
            "status": "active",
            "claimed_at": _iso(5),
            "last_updated": _iso(5),
        }])
        expired = tracker.expire_stalled(timeout_minutes=30)
        assert expired == []
        # still active
        assert tracker.active_count() == 1

    def test_old_active_session_gets_expired(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        _write_sessions(tracker, [{
            "session_id": "s1",
            "task_id": "TASK-001",
            "status": "active",
            "claimed_at": _iso(120),
            "last_updated": _iso(120),
        }])
        expired = tracker.expire_stalled(timeout_minutes=90)
        assert len(expired) == 1
        assert expired[0]["session_id"] == "s1"
        # status should be changed to stalled
        data = tracker._load()
        entry = next(s for s in data["sessions"] if s["session_id"] == "s1")
        assert entry["status"] == "stalled"
        assert "stall_reason" in entry
        # active count drops to 0
        assert tracker.active_count() == 0

    def test_completed_session_not_expired(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        _write_sessions(tracker, [{
            "session_id": "s1",
            "task_id": "TASK-001",
            "status": "completed",
            "claimed_at": _iso(200),
            "last_updated": _iso(200),
        }])
        expired = tracker.expire_stalled(timeout_minutes=30)
        assert expired == []

    def test_mixed_sessions_only_old_active_expired(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        _write_sessions(tracker, [
            {
                "session_id": "fresh",
                "task_id": "TASK-001",
                "status": "active",
                "claimed_at": _iso(10),
                "last_updated": _iso(10),
            },
            {
                "session_id": "stale",
                "task_id": "TASK-002",
                "status": "active",
                "claimed_at": _iso(200),
                "last_updated": _iso(200),
            },
        ])
        expired = tracker.expire_stalled(timeout_minutes=90)
        assert len(expired) == 1
        assert expired[0]["session_id"] == "stale"
        assert tracker.active_count() == 1  # "fresh" still active

    def test_session_with_no_last_updated_is_expired(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        _write_sessions(tracker, [{
            "session_id": "s1",
            "task_id": "TASK-001",
            "status": "active",
            "claimed_at": _iso(200),
            "last_updated": None,
        }])
        expired = tracker.expire_stalled(timeout_minutes=30)
        # missing timestamp → treated as stalled
        assert len(expired) == 1

    def test_idempotent_on_already_stalled(self, tmp_path):
        """Calling expire_stalled twice on the same session is safe."""
        tracker = _make_tracker(tmp_path)
        _write_sessions(tracker, [{
            "session_id": "s1",
            "task_id": "TASK-001",
            "status": "active",
            "claimed_at": _iso(200),
            "last_updated": _iso(200),
        }])
        first = tracker.expire_stalled(timeout_minutes=90)
        assert len(first) == 1
        # second call: session is now "stalled", not "active" → nothing more to expire
        second = tracker.expire_stalled(timeout_minutes=90)
        assert second == []


# ── active_count after expiry ─────────────────────────────────────────────────

class TestActiveCountAfterExpiry:
    def test_active_count_does_not_include_stalled(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        _write_sessions(tracker, [
            {"session_id": "a", "status": "active",    "claimed_at": _iso(5),   "last_updated": _iso(5)},
            {"session_id": "b", "status": "active",    "claimed_at": _iso(200), "last_updated": _iso(200)},
            {"session_id": "c", "status": "completed", "claimed_at": _iso(10),  "last_updated": _iso(10)},
        ])
        tracker.expire_stalled(timeout_minutes=90)
        assert tracker.active_count() == 1  # only "a" remains active
