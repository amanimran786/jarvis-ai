"""
Unit tests for proactive_watcher.py.

Covers:
  - Alert buffer: push, dedup, get_alerts, dismiss_alert
  - Urgency pattern matching (_URGENT_RE)
  - Calendar check (google_services mocked)
  - Email check (google_services mocked)
  - start/stop/status lifecycle
"""

import sys
import os
import datetime
import threading
import time
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import proactive_watcher as pw


def _reset():
    pw._alerts.clear()
    pw._seen_keys.clear()


class AlertBufferTests(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_push_adds_alert(self):
        alert = pw._push("calendar", "cal:key1", "Meeting in 5 min")
        self.assertIsNotNone(alert)
        self.assertEqual(alert["kind"], "calendar")
        self.assertFalse(alert["dismissed"])
        self.assertIn("Meeting", alert["message"])

    def test_push_deduplicates(self):
        pw._push("calendar", "cal:key1", "Meeting in 5 min")
        second = pw._push("calendar", "cal:key1", "Meeting in 4 min")
        self.assertIsNone(second)
        self.assertEqual(len(pw._alerts), 1)

    def test_get_alerts_excludes_dismissed(self):
        pw._push("calendar", "cal:key1", "Event A")
        pw._push("email", "email:key2", "Urgent email")
        pw._alerts[0]["dismissed"] = True
        result = pw.get_alerts()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["kind"], "email")

    def test_get_alerts_include_dismissed(self):
        pw._push("calendar", "cal:key1", "Event A")
        pw._alerts[0]["dismissed"] = True
        result = pw.get_alerts(include_dismissed=True)
        self.assertEqual(len(result), 1)

    def test_dismiss_alert(self):
        alert = pw._push("email", "email:key3", "Urgent!")
        dismissed = pw.dismiss_alert(alert["id"])
        self.assertTrue(dismissed)
        self.assertEqual(len(pw.get_alerts()), 0)

    def test_dismiss_nonexistent_returns_false(self):
        self.assertFalse(pw.dismiss_alert("no-such-id"))

    def test_alert_has_required_fields(self):
        alert = pw._push("calendar", "cal:fields", "Stand-up in 3 min")
        for field in ("id", "kind", "key", "message", "timestamp", "dismissed"):
            self.assertIn(field, alert)

    def test_multiple_alerts_different_keys(self):
        pw._push("calendar", "cal:a", "Event A")
        pw._push("email", "email:b", "Urgent B")
        self.assertEqual(len(pw.get_alerts()), 2)


class UrgencyPatternTests(unittest.TestCase):
    def test_urgent_matches(self):
        self.assertTrue(pw._URGENT_RE.search("URGENT: Please respond"))

    def test_action_required_matches(self):
        self.assertTrue(pw._URGENT_RE.search("Action Required: Contract renewal"))

    def test_deadline_matches(self):
        self.assertTrue(pw._URGENT_RE.search("Deadline today for submission"))

    def test_asap_matches(self):
        self.assertTrue(pw._URGENT_RE.search("Need this ASAP"))

    def test_critical_matches(self):
        self.assertTrue(pw._URGENT_RE.search("Critical system failure"))

    def test_overdue_matches(self):
        self.assertTrue(pw._URGENT_RE.search("Your task is overdue"))

    def test_response_needed_matches(self):
        self.assertTrue(pw._URGENT_RE.search("Response needed by Friday"))

    def test_normal_email_no_match(self):
        self.assertFalse(pw._URGENT_RE.search("Newsletter: weekly digest"))

    def test_case_insensitive(self):
        self.assertTrue(pw._URGENT_RE.search("urgent: review by eod"))
        self.assertTrue(pw._URGENT_RE.search("DEADLINE: submit now"))


class CalendarCheckTests(unittest.TestCase):
    def setUp(self):
        _reset()

    def _make_fake_service(self, events):
        fake_service = mock.MagicMock()
        fake_service.events().list().execute.return_value = {"items": events}
        return fake_service

    def test_upcoming_event_adds_alert(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        start_dt = now + datetime.timedelta(minutes=5)
        fake_event = {"summary": "Stand-up", "start": {"dateTime": start_dt.isoformat()}}

        with mock.patch("google_services._calendar", return_value=self._make_fake_service([fake_event])), \
             mock.patch("proactive_watcher._send_notification"):
            pw._check_calendar()

        alerts = pw.get_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "calendar")
        self.assertIn("Stand-up", alerts[0]["message"])

    def test_minutes_away_in_message(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        start_dt = now + datetime.timedelta(minutes=7)
        fake_event = {"summary": "Sync", "start": {"dateTime": start_dt.isoformat()}}

        with mock.patch("google_services._calendar", return_value=self._make_fake_service([fake_event])), \
             mock.patch("proactive_watcher._send_notification"):
            pw._check_calendar()

        msg = pw.get_alerts()[0]["message"]
        self.assertIn("min", msg)

    def test_no_events_no_alerts(self):
        with mock.patch("google_services._calendar", return_value=self._make_fake_service([])):
            pw._check_calendar()
        self.assertEqual(len(pw.get_alerts()), 0)

    def test_notification_sent_for_new_event(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        start_dt = now + datetime.timedelta(minutes=3)
        fake_event = {"summary": "Review", "start": {"dateTime": start_dt.isoformat()}}

        with mock.patch("google_services._calendar", return_value=self._make_fake_service([fake_event])):
            with mock.patch("proactive_watcher._send_notification") as mock_notify:
                pw._check_calendar()
        mock_notify.assert_called_once()

    def test_duplicate_event_no_second_notification(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        start_dt = now + datetime.timedelta(minutes=3)
        fake_event = {"summary": "Review", "start": {"dateTime": start_dt.isoformat()}}

        with mock.patch("google_services._calendar", return_value=self._make_fake_service([fake_event])), \
             mock.patch("proactive_watcher._send_notification"):
            pw._check_calendar()
            pw._check_calendar()

        self.assertEqual(len(pw.get_alerts()), 1)

    def test_all_day_event_handled(self):
        fake_event = {"summary": "Holiday", "start": {"date": "2026-04-27"}}

        with mock.patch("google_services._calendar", return_value=self._make_fake_service([fake_event])), \
             mock.patch("proactive_watcher._send_notification"):
            pw._check_calendar()

        alerts = pw.get_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn("all day", alerts[0]["message"])

    def test_exception_does_not_raise(self):
        with mock.patch("google_services._calendar", side_effect=Exception("no creds")):
            pw._check_calendar()  # must not propagate
        self.assertEqual(len(pw.get_alerts()), 0)


class EmailCheckTests(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_urgent_email_adds_alert(self):
        fake_emails = [
            {"sender": "Boss", "subject": "URGENT: Response needed", "snippet": "Please respond ASAP"}
        ]
        with mock.patch("google_services.get_unread_email_subjects", return_value=fake_emails), \
             mock.patch("proactive_watcher._send_notification"):
            pw._check_emails()

        alerts = pw.get_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "email")
        self.assertIn("Boss", alerts[0]["message"])

    def test_non_urgent_email_ignored(self):
        fake_emails = [
            {"sender": "Newsletter", "subject": "Weekly digest", "snippet": "Top stories"}
        ]
        with mock.patch("google_services.get_unread_email_subjects", return_value=fake_emails):
            pw._check_emails()
        self.assertEqual(len(pw.get_alerts()), 0)

    def test_urgency_in_snippet_triggers_alert(self):
        fake_emails = [
            {"sender": "Alice", "subject": "Meeting notes", "snippet": "Action required by EOD"}
        ]
        with mock.patch("google_services.get_unread_email_subjects", return_value=fake_emails), \
             mock.patch("proactive_watcher._send_notification"):
            pw._check_emails()
        self.assertEqual(len(pw.get_alerts()), 1)

    def test_multiple_urgent_emails(self):
        fake_emails = [
            {"sender": "A", "subject": "Urgent: A", "snippet": ""},
            {"sender": "B", "subject": "Deadline: B", "snippet": ""},
        ]
        with mock.patch("google_services.get_unread_email_subjects", return_value=fake_emails), \
             mock.patch("proactive_watcher._send_notification"):
            pw._check_emails()
        self.assertEqual(len(pw.get_alerts()), 2)

    def test_exception_does_not_raise(self):
        with mock.patch("google_services.get_unread_email_subjects", side_effect=Exception("auth")):
            pw._check_emails()  # must not propagate


class WatcherLifecycleTests(unittest.TestCase):
    def setUp(self):
        pw.stop()
        time.sleep(0.05)
        pw._stop_event.clear()
        pw._thread = None
        pw._run_count = 0

    def test_not_running_before_start(self):
        self.assertFalse(pw.status()["running"])

    def test_start_creates_daemon_thread(self):
        if not pw._ENABLED:
            self.skipTest("proactive watcher disabled via env")
        pw.start()
        time.sleep(0.1)
        self.assertTrue(pw.status()["running"])
        pw.stop()

    def test_start_idempotent(self):
        if not pw._ENABLED:
            self.skipTest("proactive watcher disabled via env")
        pw.start()
        pw.start()
        time.sleep(0.1)
        threads = [t for t in threading.enumerate() if t.name == "jarvis-proactive"]
        self.assertLessEqual(len(threads), 1)
        pw.stop()

    def test_stop_stops_thread(self):
        if not pw._ENABLED:
            self.skipTest("proactive watcher disabled via env")
        pw.start()
        time.sleep(0.1)
        pw.stop()
        time.sleep(0.15)
        self.assertFalse(pw.status()["running"])

    def test_status_fields_present(self):
        s = pw.status()
        for key in ("enabled", "running", "poll_interval_sec", "calendar_lookahead_min",
                    "run_count", "active_alerts", "total_alerts", "seen_keys"):
            self.assertIn(key, s)

    def test_status_defaults(self):
        s = pw.status()
        self.assertIsInstance(s["poll_interval_sec"], int)
        self.assertIsInstance(s["calendar_lookahead_min"], int)
        self.assertIsInstance(s["run_count"], int)
        self.assertGreaterEqual(s["poll_interval_sec"], 1)
        self.assertGreaterEqual(s["calendar_lookahead_min"], 1)


if __name__ == "__main__":
    unittest.main()
