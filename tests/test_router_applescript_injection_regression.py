"""Regression tests for the router.py AppleScript-injection related fixes:

  - `_looks_like_contact_name` must not treat any string containing "@" as a
    plausible contact/recipient name -- it has to look like an actual email
    address, otherwise a crafted string such as
    `x@" & (do shell script "...") & "` could be routed downstream as a
    "recipient" and reach messages.py's AppleScript interpolation.
  - `_schedule_osascript_alarm` must not let a raw newline in the reminder
    title break the generated AppleScript (a newline ends the `delay N`
    line early and corrupts the script), and must escape the title using
    the same backslash-first convention as the rest of the codebase.

No live osascript/subprocess calls happen in these tests; `subprocess.Popen`
is mocked.
"""
import unittest
from unittest.mock import patch

import router


class LooksLikeContactNameEmailGateTests(unittest.TestCase):
    def test_rejects_applescript_injection_shaped_string(self):
        malicious = 'x@" & (do shell script "id") & "'
        self.assertFalse(router._looks_like_contact_name(malicious))

    def test_rejects_bare_at_symbol_without_email_shape(self):
        self.assertFalse(router._looks_like_contact_name("hey @ you"))

    def test_accepts_plausible_email_address(self):
        self.assertTrue(router._looks_like_contact_name("john.doe@example.com"))


class ScheduleOsascriptAlarmTests(unittest.TestCase):
    def test_strips_newline_from_title_before_building_script(self):
        import datetime
        dt = datetime.datetime.now() + datetime.timedelta(minutes=5)
        captured = {}

        def _fake_popen(args, **kwargs):
            captured["script"] = args[-1]
            return None

        with patch("subprocess.Popen", side_effect=_fake_popen):
            router._schedule_osascript_alarm("call mom\nand also do shell script \"id\"", dt)

        script = captured["script"]
        # The script must be a single logical AppleScript with no stray
        # newline injected by the title into the `delay N` line.
        lines = script.split("\n")
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("delay "))
        self.assertIn("display notification", lines[1])

    def test_escapes_quotes_and_backslashes_in_title(self):
        import datetime
        dt = datetime.datetime.now() + datetime.timedelta(minutes=5)
        captured = {}

        def _fake_popen(args, **kwargs):
            captured["script"] = args[-1]
            return None

        with patch("subprocess.Popen", side_effect=_fake_popen):
            router._schedule_osascript_alarm('say "hi" \\ bye', dt)

        self.assertIn('display notification "say \\"hi\\" \\\\ bye"', captured["script"])


if __name__ == "__main__":
    unittest.main()
