import json
import unittest
from unittest.mock import patch

import osint_tools


class OsintToolsTests(unittest.TestCase):
    def test_username_lookup_rejects_invalid_username(self):
        result = osint_tools.username_lookup("bad user!")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_username")

    def test_username_lookup_reports_missing_tool(self):
        with patch("osint_tools.shutil.which", return_value=None):
            result = osint_tools.username_lookup("aman")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "tool_missing")

    def test_username_lookup_parses_stdout_json(self):
        payload = {
            "sites": {
                "github": {"status": "Claimed", "url_user": "https://github.com/aman"},
                "twitter": {"status": "Available", "url_user": "https://x.com/aman"},
            }
        }
        with patch("osint_tools.shutil.which", return_value="/usr/local/bin/maigret"), \
             patch("osint_tools._run_command", return_value=(0, json.dumps(payload), "")):
            result = osint_tools.username_lookup("aman")
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "maigret")
        self.assertEqual(result["found_count"], 1)
        self.assertEqual(result["profiles"][0]["site"], "github")

    def test_domain_typo_scan_rejects_invalid_domain(self):
        result = osint_tools.domain_typo_scan("bad domain")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_domain")

    def test_domain_typo_scan_parses_candidates(self):
        payload = [
            {"fuzzer": "homoglyph", "domain": "examp1e.com", "dns-a": ["1.2.3.4"], "mx": [], "whois-created": "2024-01-01"},
            {"fuzzer": "original", "domain": "example.com", "dns-a": ["1.1.1.1"]},
            {"fuzzer": "addition", "domain": "examplee.com"},
        ]
        with patch("osint_tools.shutil.which", return_value="/usr/local/bin/dnstwist"), \
             patch("osint_tools._run_command", return_value=(0, json.dumps(payload), "")):
            result = osint_tools.domain_typo_scan("example.com")
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "dnstwist")
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["candidates"][0]["domain"], "examp1e.com")


if __name__ == "__main__":
    unittest.main()
