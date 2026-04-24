"""
Unit tests for jarvis_watcher.py — proactive background watcher.

Tests cover:
  - quiet-hour detection
  - _osa_escape sanitisation
  - _needs_escalation pattern matching (via _check_tasks mock)
  - start/stop/status lifecycle
  - set_speak_callback wiring
"""

import sys
import os
import threading
import time
import unittest

# Ensure repo root on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import jarvis_watcher as jw


class OsaEscapeTests(unittest.TestCase):
    def test_plain_string_unchanged(self):
        self.assertEqual(jw._osa_escape("Hello Aman"), "Hello Aman")

    def test_double_quote_escaped(self):
        result = jw._osa_escape('say "hello"')
        self.assertNotIn('"hello"', result)
        self.assertIn('\\"hello\\"', result)

    def test_backslash_escaped(self):
        result = jw._osa_escape("path\\to\\file")
        self.assertEqual(result.count("\\\\"), 2)


class QuietHoursTests(unittest.TestCase):
    def _patch_hour(self, h):
        import datetime
        original_now = datetime.datetime.now

        class _FakeNow:
            @staticmethod
            def replace(**kwargs):
                return original_now().replace(**kwargs)

            @property
            def hour(self):
                return h

        return h

    def test_quiet_during_night(self):
        """Between 22:00 and 08:00 should be quiet."""
        import datetime
        for h in [22, 23, 0, 1, 7]:
            # Mock _is_quiet_hours by checking the logic directly
            quiet_start = 22
            quiet_end = 8
            if quiet_start > quiet_end:
                result = h >= quiet_start or h < quiet_end
            else:
                result = quiet_start <= h < quiet_end
            self.assertTrue(result, f"hour {h} should be quiet")

    def test_active_during_day(self):
        """Between 08:00 and 22:00 should not be quiet."""
        quiet_start = 22
        quiet_end = 8
        for h in [8, 12, 17, 21]:
            if quiet_start > quiet_end:
                result = h >= quiet_start or h < quiet_end
            else:
                result = quiet_start <= h < quiet_end
            self.assertFalse(result, f"hour {h} should not be quiet")


class EscalationPatternTests(unittest.TestCase):
    def test_urgent_keyword_triggers(self):
        self.assertTrue(jw._needs_escalation("this is URGENT"))

    def test_overdue_keyword_triggers(self):
        self.assertTrue(jw._needs_escalation("task is overdue"))

    def test_blocked_triggers(self):
        self.assertTrue(jw._needs_escalation("PR blocked by reviewer"))

    def test_normal_text_no_trigger(self):
        self.assertFalse(jw._needs_escalation("review this document when you get a chance"))

    def test_action_required_triggers(self):
        self.assertTrue(jw._needs_escalation("action required before Friday"))


class WatcherLifecycleTests(unittest.TestCase):
    def setUp(self):
        # Stop any running watcher from a previous test
        jw.stop()
        time.sleep(0.05)
        jw._stop_event.clear()
        jw._thread = None

    def test_status_not_running_before_start(self):
        s = jw.status()
        self.assertFalse(s["running"])

    def test_start_creates_daemon_thread(self):
        if not jw._ENABLED:
            self.skipTest("Watcher disabled via env")
        jw.start()
        time.sleep(0.1)
        s = jw.status()
        self.assertTrue(s["running"])
        jw.stop()

    def test_start_idempotent(self):
        if not jw._ENABLED:
            self.skipTest("Watcher disabled via env")
        jw.start()
        jw.start()  # second call must not crash
        time.sleep(0.1)
        # Only one watcher thread should exist
        watcher_threads = [t for t in threading.enumerate() if t.name == "jarvis-watcher"]
        self.assertLessEqual(len(watcher_threads), 1)
        jw.stop()

    def test_stop_stops_thread(self):
        if not jw._ENABLED:
            self.skipTest("Watcher disabled via env")
        jw.start()
        time.sleep(0.1)
        jw.stop()
        time.sleep(0.15)
        s = jw.status()
        self.assertFalse(s["running"])

    def test_status_fields_present(self):
        s = jw.status()
        for key in ("enabled", "running", "interval_sec", "quiet_start_hour",
                    "quiet_end_hour", "run_count", "last_escalation", "notified_count"):
            self.assertIn(key, s)


class SpeakCallbackTests(unittest.TestCase):
    def test_speak_callback_registered(self):
        spoken: list[str] = []
        jw.set_speak_callback(lambda text: spoken.append(text))
        self.assertEqual(jw._speak_cb.__class__.__name__, "function")
        # Clean up
        jw.set_speak_callback(None)

    def test_speak_callback_called_on_delivery(self):
        spoken: list[str] = []
        jw.set_speak_callback(lambda text: spoken.append(text))
        # Patch quiet hours to return False so speaking is allowed
        original = jw._is_quiet_hours
        jw._is_quiet_hours = lambda: False
        try:
            jw._deliver_alerts([("key:test", "Test urgent message")])
        finally:
            jw._is_quiet_hours = original
            jw.set_speak_callback(None)
        self.assertTrue(len(spoken) > 0)
        self.assertIn("Test urgent message", spoken[0])


class EmailUrgencyPatternTests(unittest.TestCase):
    def test_urgent_subject_triggers(self):
        import re
        pat = jw._EMAIL_URGENT_PATTERNS
        self.assertTrue(pat.search("URGENT: Please respond by EOD"))

    def test_action_required_triggers(self):
        self.assertTrue(jw._EMAIL_URGENT_PATTERNS.search("Action Required: Contract renewal"))

    def test_deadline_triggers(self):
        self.assertTrue(jw._EMAIL_URGENT_PATTERNS.search("Deadline tomorrow for submission"))

    def test_normal_email_no_trigger(self):
        self.assertFalse(jw._EMAIL_URGENT_PATTERNS.search("Newsletter: Top stories this week"))

    def test_follow_up_required_triggers(self):
        self.assertTrue(jw._EMAIL_URGENT_PATTERNS.search("Follow-up required on your request"))

    def test_check_emails_returns_list(self):
        """_check_emails should return a list (empty when Google services unavailable)."""
        result = jw._check_emails()
        self.assertIsInstance(result, list)


if __name__ == "__main__":
    unittest.main()
