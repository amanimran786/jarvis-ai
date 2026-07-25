import unittest
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import messages


class MessagesContactTests(unittest.TestCase):
    def setUp(self):
        self._alias_tmp = TemporaryDirectory()
        self.addCleanup(self._alias_tmp.cleanup)
        self._alias_patch = patch.object(
            messages,
            "_ALIASES_PATH",
            Path(self._alias_tmp.name) / "contact_aliases.json",
        )
        self._alias_patch.start()
        self.addCleanup(self._alias_patch.stop)

    def tearDown(self):
        messages._last_contact_choices = []
        messages._last_fuzzy_matches = []
        messages._last_applescript_error = ""
        messages._last_chat_db_access_error = ""
        messages._messages_history_access_prompt_shown = False

    def test_lookup_contact_auto_resolves_single_reachable_duplicate(self):
        applescript_output = "__MULTI__\nAman Imran\t(510) 753-0173\nAman Imran\nAman Imran"
        with patch("messages._run_applescript", return_value=(applescript_output, "")):
            result = messages.lookup_contact("Aman Imran")
        self.assertEqual(result, "5107530173")
        self.assertEqual(messages.get_last_contact_options(), [])

    def test_lookup_contact_auto_resolves_duplicate_rows_with_same_handle(self):
        applescript_output = (
            "__MULTI__\n"
            "Fiza Imran\tphone\t_$!<Home>!$_\t(510) 555-0123\n"
            "Fiza Imran\tphone\t_$!<Phone>!$_\t510-555-0123"
        )
        with patch("messages._run_applescript", return_value=(applescript_output, "")):
            result = messages.lookup_contact("Fiza Imran")
        self.assertEqual(result, "5105550123")
        self.assertEqual(messages.get_last_contact_options(), [])

    def test_lookup_contact_auto_resolves_duplicate_rows_with_us_country_code(self):
        applescript_output = (
            "__MULTI__\n"
            "Fiza Imran\tphone\t_$!<Phone>!$_\t+1 (510) 555-0123\n"
            "Fiza Imran\tphone\t_$!<Home>!$_\t510-555-0123"
        )
        with patch("messages._run_applescript", return_value=(applescript_output, "")):
            result = messages.lookup_contact("Fiza Imran")
        self.assertEqual(result, "+15105550123")
        self.assertEqual(messages.get_last_contact_options(), [])

    def test_lookup_contact_only_exposes_reachable_duplicate_choices(self):
        applescript_output = "__MULTI__\nAman Imran\tphone\t_$!<Home>!$_\t(510) 753-0173\nAman Imran\temail\t_$!<Work>!$_\taman@example.com\nAman Imran"
        with patch("messages._run_applescript", return_value=(applescript_output, "")):
            result = messages.lookup_contact("Aman Imran")
        self.assertEqual(result, messages._AMBIGUOUS_CONTACT)
        self.assertEqual(
            messages.get_last_contact_options(),
            ["Aman Imran (home phone, ending 0173)", "Aman Imran (work email, am***@example.com)"],
        )

    def test_lookup_contact_reports_when_no_duplicate_has_handle(self):
        applescript_output = "__MULTI__\nAman Imran\nAman Imran"
        with patch("messages._run_applescript", return_value=(applescript_output, "")):
            result = messages.lookup_contact("Aman Imran")
        self.assertEqual(result, messages._CONTACT_WITHOUT_HANDLE)
        self.assertEqual(messages.get_last_contact_options(), [])

    def test_send_imessage_reports_when_matching_contacts_have_no_handles(self):
        with patch("messages.lookup_contact", return_value=messages._CONTACT_WITHOUT_HANDLE):
            result = messages.send_imessage("Aman Imran", "hello")
        self.assertIn("none of them have a phone number or email", result.lower())

    def test_send_imessage_rejects_applescript_injection_recipient(self):
        """A recipient shaped like `x@" & (do shell script "...") & "` must never be
        interpolated raw into the AppleScript buddy literal. It doesn't match the
        strict phone/email allowlist, so it must fall through to contact lookup
        (which fails here) instead of being treated as a direct address."""
        malicious = 'x@" & (do shell script "id") & "'
        with patch("messages.lookup_contact", return_value=None), \
             patch("messages._run_applescript") as mock_applescript:
            result = messages.send_imessage(malicious, "hello")
        mock_applescript.assert_not_called()
        self.assertIn("couldn't find a contact", result.lower())

    def test_send_imessage_escapes_quotes_and_backslashes_in_body(self):
        """Body containing both a quote and a backslash must escape backslash
        first so the resulting AppleScript string stays balanced (regression
        for the reversed-escape-order bug)."""
        captured = {}

        def _fake_run_applescript(script):
            captured["script"] = script
            return "", ""

        with patch("messages._run_applescript", side_effect=_fake_run_applescript):
            messages.send_imessage("+15105550123", 'He said "hi" \\ bye')

        script = captured["script"]
        # The send "..." literal must contain a properly escaped body: the
        # backslash is doubled first, then the quotes are escaped, so no raw
        # unescaped quote terminates the string early.
        self.assertIn('send "He said \\"hi\\" \\\\ bye" to targetBuddy', script)

    def test_send_imessage_escapes_valid_direct_address_before_interpolation(self):
        """Even an address that passes the strict allowlist is still run through
        the AppleScript escaper before interpolation (defense in depth)."""
        captured = {}

        def _fake_run_applescript(script):
            captured["script"] = script
            return "", ""

        with patch("messages._run_applescript", side_effect=_fake_run_applescript):
            messages.send_imessage("+1 (510) 555-0123", "hi")

        self.assertIn('buddy "+1 (510) 555-0123" of targetService', captured["script"])

    def test_is_valid_direct_address_rejects_injection_shapes(self):
        self.assertFalse(messages._is_valid_direct_address('x@" & (do shell script "id") & "'))
        self.assertFalse(messages._is_valid_direct_address('555" & (do shell script "id") & "'))
        self.assertTrue(messages._is_valid_direct_address("+15105550123"))
        self.assertTrue(messages._is_valid_direct_address("user@example.com"))

    def test_describe_contact_handles_formats_phone_and_email_labels(self):
        with patch(
            "messages._collect_contact_rows",
            return_value=[
                {"name": "Dad", "kind": "phone", "label": "home", "value": "(510) 828-8207"},
                {"name": "Dad", "kind": "email", "label": "work", "value": "dad@example.com"},
            ],
        ):
            text = messages.describe_contact_handles("Dad")
        self.assertIn("Dad: home phone (510) 828-8207", text)
        self.assertIn("Dad: work email dad@example.com", text)

    def test_applescript_timeout_returns_readable_error(self):
        with patch("messages.subprocess.run", side_effect=subprocess.TimeoutExpired(["osascript"], 10)):
            out, err = messages._run_applescript("tell application \"Contacts\" to return \"\"")
        self.assertEqual(out, "")
        self.assertIn("took too long", err)

    def test_describe_contact_handles_surfaces_contact_timeout(self):
        with patch("messages._collect_contact_rows", return_value=[]), \
             patch("messages.list_contacts_fuzzy", return_value=[]):
            messages._last_applescript_error = "macOS Contacts or Messages took too long to respond."
            text = messages.describe_contact_handles("Dad")
        self.assertIn("couldn't read contact handles", text.lower())
        self.assertIn("took too long", text.lower())

    def test_messages_history_permission_text_surfaces_full_disk_access_hint(self):
        missing_path = Path("/tmp/jarvis_missing_messages_chat.db")
        text = messages.messages_history_permission_text(missing_path)

        self.assertIn("Full Disk Access", text)
        self.assertIn("Terminal", text)
        self.assertIn("Jarvis.app", text)
        self.assertIn(str(missing_path), text)

    def test_read_recent_thread_surfaces_chat_db_access_prompt_once(self):
        with patch("messages._copy_chat_db_snapshot", return_value=False), \
             patch("messages_thread.get_thread", return_value=[]):
            messages._mark_chat_db_access_error("operation not permitted")

            first = messages.read_recent_thread("+15105550179")
            messages._mark_chat_db_access_error("operation not permitted")
            second = messages.read_recent_thread("+15105550179")

        self.assertIn("Full Disk Access", first)
        self.assertIn("Terminal", first)
        self.assertIn("operation not permitted", first)
        self.assertEqual(second, "No recent messages with +15105550179.")

    def test_read_recent_thread_appends_one_time_prompt_to_fallback_thread(self):
        fallback = [{"direction": "in", "body": "got it"}]
        with patch("messages._copy_chat_db_snapshot", return_value=False), \
             patch("messages_thread.get_thread", return_value=fallback):
            messages._mark_chat_db_access_error("operation not permitted")

            first = messages.read_recent_thread("+15105550179")
            messages._mark_chat_db_access_error("operation not permitted")
            second = messages.read_recent_thread("+15105550179")

        self.assertIn("[+15105550179] got it", first)
        self.assertIn("Full Disk Access", first)
        self.assertEqual(second, "[+15105550179] got it")

    def test_messages_history_access_status_reads_sqlite_database(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "chat.db"
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.execute("create table message (text text)")
            conn.close()

            status = messages.messages_history_access_status(db_path)

        self.assertTrue(status["ok"])
        self.assertEqual(status["path"], str(db_path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
