"""Tests for harness/loop_watchdog.py — resume signal and heartbeat."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import loop_watchdog


class ResumeSignalTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._signal = Path(self._tmpdir.name) / "RESUME_SIGNAL.json"
        self._patcher = patch("harness.loop_watchdog._SIGNAL_PATH", self._signal)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_no_signal_returns_none(self):
        result = loop_watchdog.check_resume_signal()
        self.assertIsNone(result)

    def test_resume_signal_returns_reason(self):
        self._signal.write_text(json.dumps({"signal": "resume", "reason": "rate limit reset"}))
        result = loop_watchdog.check_resume_signal()
        self.assertEqual(result, "rate limit reset")

    def test_resume_signal_consumed_after_check(self):
        self._signal.write_text(json.dumps({"signal": "resume", "reason": "test"}))
        loop_watchdog.check_resume_signal()
        self.assertFalse(self._signal.exists())

    def test_unknown_signal_type_consumed(self):
        self._signal.write_text(json.dumps({"signal": "pause", "reason": "test"}))
        result = loop_watchdog.check_resume_signal()
        self.assertIsNone(result)
        self.assertFalse(self._signal.exists())

    def test_malformed_json_does_not_raise(self):
        self._signal.write_text("not valid json")
        result = loop_watchdog.check_resume_signal()
        self.assertIsNone(result)

    def test_prints_watchdog_message(self):
        self._signal.write_text(json.dumps({"signal": "resume", "reason": "continue after pause"}))
        with patch("builtins.print") as mock_print:
            loop_watchdog.check_resume_signal()
        mock_print.assert_called_once()
        printed = mock_print.call_args[0][0]
        self.assertIn("[WATCHDOG]", printed)
        self.assertIn("continue after pause", printed)


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._status = Path(self._tmpdir.name) / "ORCHESTRATOR_STATUS.json"
        self._patcher = patch("harness.loop_watchdog._STATUS_PATH", self._status)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def _read_entry(self) -> dict:
        data = json.loads(self._status.read_text())
        sessions = data.get("sessions", [])
        if isinstance(sessions, list):
            return next((s for s in sessions if s.get("session_id") == loop_watchdog._SESSION_KEY), {})
        return sessions.get(loop_watchdog._SESSION_KEY, {})

    def test_creates_status_file_when_absent(self):
        self.assertFalse(self._status.exists())
        loop_watchdog.heartbeat("test task")
        self.assertTrue(self._status.exists())

    def test_heartbeat_sets_current_task(self):
        loop_watchdog.heartbeat("building /score command")
        entry = self._read_entry()
        self.assertEqual(entry["current_task"], "building /score command")

    def test_heartbeat_sets_status(self):
        loop_watchdog.heartbeat("done", status="idle")
        entry = self._read_entry()
        self.assertEqual(entry["status"], "idle")

    def test_heartbeat_sets_last_active_timestamp(self):
        loop_watchdog.heartbeat("task")
        entry = self._read_entry()
        self.assertIn("last_active", entry)
        self.assertTrue(entry["last_active"].endswith("Z"))

    def test_heartbeat_updates_existing_entry(self):
        loop_watchdog.heartbeat("task A")
        loop_watchdog.heartbeat("task B")
        entry = self._read_entry()
        self.assertEqual(entry["current_task"], "task B")
        # Only one entry per session key
        data = json.loads(self._status.read_text())
        sessions = data.get("sessions", [])
        matching = [s for s in sessions if s.get("session_id") == loop_watchdog._SESSION_KEY]
        self.assertEqual(len(matching), 1)

    def test_heartbeat_preserves_existing_sessions(self):
        # Pre-populate with another session
        self._status.write_text(json.dumps({
            "sessions": [{"session_id": "other-agent", "status": "idle"}]
        }))
        loop_watchdog.heartbeat("self-eval task")
        data = json.loads(self._status.read_text())
        session_ids = {s["session_id"] for s in data["sessions"]}
        self.assertIn("other-agent", session_ids)
        self.assertIn(loop_watchdog._SESSION_KEY, session_ids)

    def test_heartbeat_handles_legacy_dict_sessions(self):
        self._status.write_text(json.dumps({"sessions": {}}))
        loop_watchdog.heartbeat("task")
        data = json.loads(self._status.read_text())
        # Should still write without error
        self.assertTrue(self._status.exists())

    def test_heartbeat_never_raises(self):
        with patch("builtins.open", side_effect=OSError("disk full")):
            loop_watchdog.heartbeat("task")  # must not raise


if __name__ == "__main__":
    unittest.main()
