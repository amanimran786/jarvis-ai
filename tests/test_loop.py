"""
tests/test_loop.py — Loop control primitives.
"""
import json
import os

import pytest


@pytest.fixture(autouse=True)
def _clean_files(tmp_path, monkeypatch):
    import harness.loop as lmod
    monkeypatch.setattr(lmod, "_signal_path", lambda: tmp_path / "RESUME_SIGNAL.json")
    monkeypatch.setattr(lmod, "_status_path", lambda: tmp_path / "ORCHESTRATOR_STATUS.json")
    yield


class TestResumeSignal:
    def test_no_file_returns_empty(self):
        from harness.loop import check_resume_signal
        assert check_resume_signal() == {}

    def test_resume_signal_is_consumed(self, tmp_path):
        import harness.loop as lmod
        sig_path = lmod._signal_path()
        sig_path.write_text(json.dumps({"signal": "resume", "reason": "test reset"}))

        result = lmod.check_resume_signal()
        assert result["signal"] == "resume"
        assert result["reason"] == "test reset"
        assert not sig_path.exists(), "signal file must be deleted after reading"

    def test_non_resume_signal_not_deleted(self, tmp_path):
        import harness.loop as lmod
        sig_path = lmod._signal_path()
        sig_path.write_text(json.dumps({"signal": "pause"}))
        lmod.check_resume_signal()
        # non-resume signals are returned but not deleted
        assert sig_path.exists()

    def test_malformed_json_returns_empty(self, tmp_path):
        import harness.loop as lmod
        lmod._signal_path().write_text("{broken json")
        result = lmod.check_resume_signal()
        assert result == {}

    def test_missing_signal_key_not_consumed(self, tmp_path):
        import harness.loop as lmod
        sig_path = lmod._signal_path()
        sig_path.write_text(json.dumps({"reason": "no signal key"}))
        lmod.check_resume_signal()
        assert sig_path.exists()


class TestHeartbeat:
    def test_creates_status_file(self, tmp_path):
        import harness.loop as lmod
        lmod.heartbeat("test task")
        path = lmod._status_path()
        assert path.exists()
        data = json.loads(path.read_text())
        sessions = data["sessions"]
        if isinstance(sessions, list):
            entry = next(s for s in sessions if s["session_id"] == "jarvis-local-llm")
        else:
            entry = sessions["jarvis-local-llm"]
        assert entry["current_task"] == "test task"
        assert entry["status"] == "active"
        assert "last_active" in entry

    def test_heartbeat_updates_existing(self, tmp_path):
        import harness.loop as lmod
        lmod.heartbeat("first task")
        lmod.heartbeat("second task")
        data = json.loads(lmod._status_path().read_text())
        sessions = data["sessions"]
        if isinstance(sessions, list):
            entry = next(s for s in sessions if s["session_id"] == "jarvis-local-llm")
        else:
            entry = sessions["jarvis-local-llm"]
        assert entry["current_task"] == "second task"

    def test_heartbeat_merges_extra(self, tmp_path):
        import harness.loop as lmod
        lmod.heartbeat("task with extra", extra={"commit": "abc123", "tests": "28/28"})
        data = json.loads(lmod._status_path().read_text())
        sessions = data["sessions"]
        if isinstance(sessions, list):
            entry = next(s for s in sessions if s["session_id"] == "jarvis-local-llm")
        else:
            entry = sessions["jarvis-local-llm"]
        assert entry["commit"] == "abc123"
        assert entry["tests"] == "28/28"

    def test_heartbeat_tolerates_corrupt_status(self, tmp_path):
        import harness.loop as lmod
        lmod._status_path().write_text("{broken")
        lmod.heartbeat("recovery task")  # must not raise

    def test_status_idle(self, tmp_path):
        import harness.loop as lmod
        lmod.heartbeat("idle check", status="idle")
        data = json.loads(lmod._status_path().read_text())
        sessions = data["sessions"]
        if isinstance(sessions, list):
            entry = next(s for s in sessions if s["session_id"] == "jarvis-local-llm")
        else:
            entry = sessions["jarvis-local-llm"]
        assert entry["status"] == "idle"
