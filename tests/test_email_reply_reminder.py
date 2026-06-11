"""
tests/test_email_reply_reminder.py

Targeted tests for:
  - Email reply flow (three-turn: intent → body → confirm send)
  - Reminder fast-path (regex parse + calendar/osascript dispatch)

All google_services calls are mocked — no real HTTP.
Uses AAA pattern throughout.
"""
import sys
import datetime
import unittest
from unittest.mock import patch, MagicMock, call

import router


# ── helpers ──────────────────────────────────────────────────────────────────

def _collect(stream_or_str):
    """Drain a generator or return a plain string."""
    if isinstance(stream_or_str, str):
        return stream_or_str
    return "".join(stream_or_str)


FAKE_EMAILS = [
    {
        "sender": "Farhan",
        "from_address": "farhan@example.com",
        "subject": "Team sync notes",
        "snippet": "Following up on yesterday's meeting.",
    },
    {
        "sender": "Alice",
        "from_address": "alice@example.com",
        "subject": "Budget proposal",
        "snippet": "Please review before Friday.",
    },
]


# ── Email Reply Flow ──────────────────────────────────────────────────────────

class TestEmailReplyFlow(unittest.TestCase):

    def setUp(self):
        router._clear_pending_email_draft()

    def tearDown(self):
        router._clear_pending_email_draft()

    # ------------------------------------------------------------------
    # Turn 1: intent detection → sets _pending_email_reply
    # ------------------------------------------------------------------

    def test_reply_to_email_from_name_sets_pending_reply(self):
        # Arrange
        with patch("router.gs.get_unread_email_subjects", return_value=FAKE_EMAILS):
            # Act
            stream, label = router.route_stream("reply to the email from Farhan")
            text = _collect(stream)

        # Assert
        self.assertEqual(label, "Gmail")
        self.assertIn("Farhan", text)
        self.assertIn("Team sync notes", text)
        self.assertTrue(router._has_pending_email_reply())
        self.assertEqual(router._pending_email_reply["from_address"], "farhan@example.com")
        self.assertEqual(router._pending_email_reply["subject"], "Re: Team sync notes")

    def test_reply_possessive_form_sets_pending_reply(self):
        # Arrange
        with patch("router.gs.get_unread_email_subjects", return_value=FAKE_EMAILS):
            # Act
            stream, label = router.route_stream("reply to Farhan's email")
            text = _collect(stream)

        # Assert
        self.assertEqual(label, "Gmail")
        self.assertTrue(router._has_pending_email_reply())
        self.assertEqual(router._pending_email_reply["from_address"], "farhan@example.com")

    def test_reply_falls_back_to_most_recent_when_no_name(self):
        # Arrange
        with patch("router.gs.get_unread_email_subjects", return_value=FAKE_EMAILS):
            # Act
            stream, label = router.route_stream("reply to that email")
            text = _collect(stream)

        # Assert
        self.assertEqual(label, "Gmail")
        self.assertTrue(router._has_pending_email_reply())
        # Falls back to first email (most recent)
        self.assertEqual(router._pending_email_reply["from_address"], "farhan@example.com")

    def test_reply_returns_no_email_message_when_inbox_empty(self):
        # Arrange
        with patch("router.gs.get_unread_email_subjects", return_value=[]):
            # Act
            stream, label = router.route_stream("reply to the email from Farhan")
            text = _collect(stream)

        # Assert
        self.assertEqual(label, "Gmail")
        self.assertFalse(router._has_pending_email_reply())
        self.assertIn("couldn't find", text.lower())

    # ------------------------------------------------------------------
    # Turn 2: user provides body → transitions to pending_email_draft
    # ------------------------------------------------------------------

    def test_body_input_clears_pending_reply_and_sets_draft(self):
        # Arrange
        router._set_pending_email_reply("Farhan", "farhan@example.com", "Re: Team sync notes")

        # Act
        stream, label = router.route_stream("Thanks for sharing! I'll follow up.")
        text = _collect(stream)

        # Assert
        self.assertEqual(label, "Gmail")
        self.assertFalse(router._has_pending_email_reply())
        self.assertTrue(router._has_pending_email_draft())
        draft = router._pending_email_draft
        self.assertEqual(draft["to"], "farhan@example.com")
        self.assertEqual(draft["subject"], "Re: Team sync notes")
        self.assertIn("follow up", draft["body"].lower())
        self.assertIn("Email draft ready", text)

    def test_body_input_confirmation_prompt_shows_reply_subject(self):
        # Arrange
        router._set_pending_email_reply("Alice", "alice@example.com", "Re: Budget proposal")

        # Act
        stream, label = router.route_stream("Looks good, let's proceed.")
        text = _collect(stream)

        # Assert
        self.assertIn("Re: Budget proposal", text)
        self.assertIn("Alice", text)

    def test_bare_yes_while_awaiting_body_re_prompts(self):
        # Arrange
        router._set_pending_email_reply("Farhan", "farhan@example.com", "Re: Team sync notes")

        # Act
        stream, label = router.route_stream("yes")
        text = _collect(stream)

        # Assert — should ask for the body, not send
        self.assertEqual(label, "Gmail")
        self.assertTrue(router._has_pending_email_reply())
        self.assertIn("Farhan", text)

    # ------------------------------------------------------------------
    # Turn 3: confirm send → calls send_email with correct args
    # ------------------------------------------------------------------

    def test_confirm_send_calls_send_email_with_reply_args(self):
        # Arrange
        router._set_pending_email_reply("Farhan", "farhan@example.com", "Re: Team sync notes")
        router.route_stream("Thanks, will do!")  # body turn — populates draft
        router._pending_email_reply = None  # ensure cleared by prior turn

        # Act
        with patch("router.gs.send_email", return_value="Email sent to farhan@example.com.") as mock_send:
            stream, label = router.route_stream("confirm send")
            text = _collect(stream)

        # Assert
        self.assertEqual(label, "Gmail")
        self.assertIn("farhan@example.com", text)
        mock_send.assert_called_once_with(
            "farhan@example.com",
            "Re: Team sync notes",
            "Thanks, will do!",
        )
        self.assertFalse(router._has_pending_email_draft())

    def test_send_it_also_confirms_and_sends(self):
        # Arrange
        router._set_pending_email_reply("Farhan", "farhan@example.com", "Re: Team sync notes")
        router.route_stream("Sounds great")  # body turn

        # Act
        with patch("router.gs.send_email", return_value="Email sent to farhan@example.com.") as mock_send:
            stream, label = router.route_stream("send it")
            _collect(stream)

        # Assert
        mock_send.assert_called_once()
        self.assertFalse(router._has_pending_email_draft())

    def test_full_three_turn_reply_flow(self):
        """End-to-end: reply to X → body → confirm send."""
        # Turn 1
        with patch("router.gs.get_unread_email_subjects", return_value=FAKE_EMAILS):
            stream, label = router.route_stream("reply to the email from Farhan")
            text = _collect(stream)
        self.assertEqual(label, "Gmail")
        self.assertIn("Farhan", text)
        self.assertTrue(router._has_pending_email_reply())

        # Turn 2
        stream, label = router.route_stream("Sure, I'll join the sync tomorrow.")
        text = _collect(stream)
        self.assertEqual(label, "Gmail")
        self.assertFalse(router._has_pending_email_reply())
        self.assertTrue(router._has_pending_email_draft())
        self.assertEqual(router._pending_email_draft["to"], "farhan@example.com")

        # Turn 3
        with patch("router.gs.send_email", return_value="Email sent to farhan@example.com.") as mock_send:
            stream, label = router.route_stream("confirm send")
            text = _collect(stream)
        self.assertEqual(label, "Gmail")
        mock_send.assert_called_once_with(
            "farhan@example.com",
            "Re: Team sync notes",
            "Sure, I'll join the sync tomorrow.",
        )
        self.assertFalse(router._has_pending_email_draft())

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------

    def test_cancel_while_awaiting_body_clears_pending_reply(self):
        # Arrange
        router._set_pending_email_reply("Farhan", "farhan@example.com", "Re: Team sync notes")

        # Act
        stream, label = router.route_stream("cancel email")
        text = _collect(stream)

        # Assert
        self.assertFalse(router._has_pending_email_reply())
        self.assertIn("Farhan", text)

    def test_gmail_unavailable_on_confirm_returns_error_message(self):
        # Arrange
        router._set_pending_email_reply("Farhan", "farhan@example.com", "Re: Team sync notes")
        router.route_stream("Alright, works for me.")  # body turn

        # Act
        with patch("router.gs.send_email", side_effect=Exception("auth error")):
            stream, label = router.route_stream("confirm send")
            text = _collect(stream)

        # Assert — error message returned; draft cleared by design (no retry needed)
        self.assertEqual(label, "Gmail")
        self.assertIn("unavailable", text.lower())


# ── Reminder Fast-Path ────────────────────────────────────────────────────────

class TestReminderFastPath(unittest.TestCase):

    def setUp(self):
        # Re-anchor sys.modules["router"] to our module object so that
        # patch("router.X") targets the same object this test file uses.
        # Digest test teardown pops sys.modules["router"], causing patch() to
        # reimport a fresh module — different object from our generator's namespace.
        sys.modules["router"] = router
        router._clear_pending_email_draft()

    # ------------------------------------------------------------------
    # _parse_calendar_reminder — unit tests on the parser
    # ------------------------------------------------------------------

    def test_parse_remind_me_at_time_to_task(self):
        # Arrange / Act
        result = router._parse_calendar_reminder("remind me at 3pm to call Farhan")

        # Assert
        self.assertIsNotNone(result)
        title, dt = result
        self.assertIn("call farhan", title.lower())
        self.assertEqual(dt.hour, 15)

    def test_parse_set_reminder_for_tomorrow_at_time_to_task(self):
        # Arrange / Act
        result = router._parse_calendar_reminder("set a reminder for tomorrow at 9am to review prs")

        # Assert
        self.assertIsNotNone(result)
        title, dt = result
        self.assertIn("review prs", title.lower())
        self.assertEqual(dt.hour, 9)
        # Must be tomorrow
        tomorrow = (datetime.datetime.now() + datetime.timedelta(days=1)).date()
        self.assertEqual(dt.date(), tomorrow)

    def test_parse_remind_me_tomorrow_at_time_to_task(self):
        # Arrange / Act
        result = router._parse_calendar_reminder("remind me tomorrow at 9am to review prs")

        # Assert
        self.assertIsNotNone(result)
        title, dt = result
        self.assertIn("review prs", title.lower())
        tomorrow = (datetime.datetime.now() + datetime.timedelta(days=1)).date()
        self.assertEqual(dt.date(), tomorrow)

    def test_parse_returns_none_when_no_time(self):
        # Arrange / Act
        result = router._parse_calendar_reminder("remind me to call farhan")

        # Assert
        self.assertIsNone(result)

    def test_parse_same_day_time_in_future(self):
        # Arrange — use a time far in the future so it lands today
        result = router._parse_calendar_reminder("remind me to wake up at 11:59 pm")

        # Assert
        self.assertIsNotNone(result)
        title, dt = result
        self.assertEqual(dt.hour, 23)
        self.assertEqual(dt.minute, 59)

    # ------------------------------------------------------------------
    # route_stream reminder integration — calendar path
    # ------------------------------------------------------------------

    def test_reminder_route_calls_create_event_with_correct_args(self):
        # Arrange
        fake_result = "Event 'call Farhan' created for 3:00 PM on June 7."

        # Act
        with patch("router.gs.create_event", return_value=fake_result) as mock_create:
            stream, label = router.route_stream("remind me at 3pm to call Farhan")
            text = _collect(stream)

        # Assert
        self.assertEqual(label, "Calendar")
        self.assertIn("call Farhan".lower(), text.lower())
        mock_create.assert_called_once()
        title_arg, dt_arg = mock_create.call_args[0]
        self.assertIn("call farhan", title_arg.lower())
        self.assertEqual(dt_arg.hour, 15)

    def test_reminder_route_tomorrow_calls_create_event_on_next_day(self):
        # Arrange
        fake_result = "Event 'review prs' created for 9:00 AM on June 8."

        # Act
        with patch("router.gs.create_event", return_value=fake_result) as mock_create:
            stream, label = router.route_stream("set a reminder for tomorrow at 9am to review PRs")
            text = _collect(stream)

        # Assert
        self.assertEqual(label, "Calendar")
        mock_create.assert_called_once()
        _, dt_arg = mock_create.call_args[0]
        tomorrow = (datetime.datetime.now() + datetime.timedelta(days=1)).date()
        self.assertEqual(dt_arg.date(), tomorrow)
        self.assertEqual(dt_arg.hour, 9)

    def test_reminder_route_falls_back_to_osascript_when_calendar_unavailable(self):
        # Arrange — calendar raises, osascript should fire
        with patch("router.gs.create_event", side_effect=Exception("auth error")), \
             patch("router._schedule_osascript_alarm", return_value="Reminder set for 3:00 PM: call Farhan.") as mock_alarm:

            # Act
            stream, label = router.route_stream("remind me at 3pm to call Farhan")
            text = _collect(stream)

        # Assert
        self.assertEqual(label, "Calendar")
        self.assertIn("Reminder set", text)
        mock_alarm.assert_called_once()

    def test_reminder_route_label_is_calendar(self):
        # Act
        with patch("router.gs.create_event", return_value="Event created."):
            stream, label = router.route_stream("remind me at 3pm to call Farhan")
            _collect(stream)

        # Assert
        self.assertEqual(label, "Calendar")


if __name__ == "__main__":
    unittest.main()
