import unittest
import subprocess
from unittest.mock import patch

import messages


class MessagesContactTests(unittest.TestCase):
    def tearDown(self):
        messages._last_contact_choices = []
        messages._last_fuzzy_matches = []
        messages._last_applescript_error = ""

    def test_lookup_contact_auto_resolves_single_reachable_duplicate(self):
        applescript_output = "__MULTI__\nAman Imran\t(510) 753-0173\nAman Imran\nAman Imran"
        with patch("messages._run_applescript", return_value=(applescript_output, "")):
            result = messages.lookup_contact("Aman Imran")
        self.assertEqual(result, "5107530173")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
