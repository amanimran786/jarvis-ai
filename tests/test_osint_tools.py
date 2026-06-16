import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import osint_tools


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_isolated_cache(tmp_path: Path) -> osint_tools._ScanCache:
    """Return a _ScanCache backed by a temp DB so tests don't pollute the real one."""
    cache = osint_tools._ScanCache.__new__(osint_tools._ScanCache)
    cache._lock = threading.Lock()
    cache._conn = sqlite3.connect(str(tmp_path / "test_osint.db"), check_same_thread=False)
    cache._conn.execute(
        "CREATE TABLE IF NOT EXISTS scans "
        "(id TEXT PRIMARY KEY, tool TEXT NOT NULL, target TEXT NOT NULL, "
        "result_json TEXT NOT NULL, created_at REAL NOT NULL)"
    )
    cache._conn.commit()
    return cache


# ---------------------------------------------------------------------------
# Username lookup (existing + cache-aware)
# ---------------------------------------------------------------------------

class OsintUsernameLookupTests(unittest.TestCase):
    def test_rejects_invalid_username(self):
        result = osint_tools.username_lookup("bad user!")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_username")

    def test_reports_missing_tool(self):
        with patch("osint_tools.shutil.which", return_value=None), \
             patch.object(osint_tools._cache, "get", return_value=None):
            result = osint_tools.username_lookup("aman")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "tool_missing")

    def test_parses_stdout_json(self):
        payload = {
            "sites": {
                "github": {"status": "Claimed", "url_user": "https://github.com/aman"},
                "twitter": {"status": "Available", "url_user": "https://x.com/aman"},
            }
        }
        with patch("osint_tools.shutil.which", return_value="/usr/local/bin/maigret"), \
             patch("osint_tools._run_command", return_value=(0, json.dumps(payload), "")), \
             patch.object(osint_tools._cache, "get", return_value=None), \
             patch.object(osint_tools._cache, "put"):
            result = osint_tools.username_lookup("aman")
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "maigret")
        self.assertEqual(result["found_count"], 1)
        self.assertEqual(result["profiles"][0]["site"], "github")
        self.assertFalse(result["cache_hit"])

    def test_returns_cache_hit_on_second_call(self):
        with tempfile.TemporaryDirectory() as td:
            cache = _make_isolated_cache(Path(td))
            profiles = [{"site": "github", "url": "https://github.com/aman", "status": "Claimed"}]
            cache.put("maigret", "aman", {"profiles": profiles})

            with patch.object(osint_tools, "_cache", cache):
                result = osint_tools.username_lookup("aman")

        self.assertTrue(result["ok"])
        self.assertTrue(result["cache_hit"])
        self.assertEqual(result["found_count"], 1)


# ---------------------------------------------------------------------------
# Domain typo scan (existing + cache-aware)
# ---------------------------------------------------------------------------

class OsintDomainTypoScanTests(unittest.TestCase):
    def test_rejects_invalid_domain(self):
        result = osint_tools.domain_typo_scan("bad domain")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_domain")

    def test_parses_candidates(self):
        payload = [
            {"fuzzer": "homoglyph", "domain": "examp1e.com", "dns-a": ["1.2.3.4"], "mx": [], "whois-created": "2024-01-01"},
            {"fuzzer": "original", "domain": "example.com", "dns-a": ["1.1.1.1"]},
            {"fuzzer": "addition", "domain": "examplee.com"},
        ]
        with patch("osint_tools.shutil.which", return_value="/usr/local/bin/dnstwist"), \
             patch("osint_tools._run_command", return_value=(0, json.dumps(payload), "")), \
             patch.object(osint_tools._cache, "get", return_value=None), \
             patch.object(osint_tools._cache, "put"):
            result = osint_tools.domain_typo_scan("example.com")
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "dnstwist")
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["candidates"][0]["domain"], "examp1e.com")
        self.assertFalse(result["cache_hit"])

    def test_returns_cache_hit_on_second_call(self):
        with tempfile.TemporaryDirectory() as td:
            cache = _make_isolated_cache(Path(td))
            candidates = [{"domain": "examp1e.com", "fuzzer": "homoglyph", "risk_score": 4}]
            cache.put("dnstwist", "example.com", {"candidates": candidates})

            with patch.object(osint_tools, "_cache", cache):
                result = osint_tools.domain_typo_scan("example.com")

        self.assertTrue(result["ok"])
        self.assertTrue(result["cache_hit"])
        self.assertEqual(result["candidate_count"], 1)


# ---------------------------------------------------------------------------
# Subdomain enumeration
# ---------------------------------------------------------------------------

class OsintSubdomainEnumTests(unittest.TestCase):
    def test_rejects_invalid_domain(self):
        result = osint_tools.subdomain_enum("not a domain")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_domain")

    def test_reports_missing_tool(self):
        with patch("osint_tools.shutil.which", return_value=None):
            result = osint_tools.subdomain_enum("example.com")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "tool_missing")

    def test_parses_json_line_output(self):
        lines = "\n".join([
            json.dumps({"host": "api.example.com", "source": "certspotter", "ip": "1.2.3.4"}),
            json.dumps({"host": "mail.example.com", "source": "dnsdumpster", "ip": ""}),
        ])
        with patch("osint_tools.shutil.which", return_value="/usr/local/bin/subfinder"), \
             patch("osint_tools._run_command", return_value=(0, lines, "")):
            result = osint_tools.subdomain_enum("example.com")
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "subfinder")
        self.assertEqual(result["count"], 2)
        hosts = [s["host"] for s in result["subdomains"]]
        self.assertIn("api.example.com", hosts)
        self.assertIn("mail.example.com", hosts)

    def test_handles_non_json_lines_gracefully(self):
        lines = "api.example.com\nmail.example.com\nnot-json-at-all"
        with patch("osint_tools.shutil.which", return_value="/usr/local/bin/subfinder"), \
             patch("osint_tools._run_command", return_value=(0, lines, "")):
            result = osint_tools.subdomain_enum("example.com")
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 3)
        hosts = [s["host"] for s in result["subdomains"]]
        self.assertIn("api.example.com", hosts)

    def test_no_active_flag_when_passive_only(self):
        # subfinder is passive by default; no flag needed for passive mode
        with patch("osint_tools.shutil.which", return_value="/usr/local/bin/subfinder") as _w, \
             patch("osint_tools._run_command", return_value=(0, "", "")) as mock_run:
            osint_tools.subdomain_enum("example.com", passive_only=True)
        argv = mock_run.call_args[0][0]
        self.assertNotIn("-passive", argv)
        self.assertNotIn("-active", argv)

    def test_active_flag_included_when_not_passive(self):
        with patch("osint_tools.shutil.which", return_value="/usr/local/bin/subfinder"), \
             patch("osint_tools._run_command", return_value=(0, "", "")) as mock_run:
            osint_tools.subdomain_enum("example.com", passive_only=False)
        argv = mock_run.call_args[0][0]
        self.assertIn("-active", argv)
        self.assertNotIn("-passive", argv)

    def test_timeout_returns_error(self):
        import subprocess
        with patch("osint_tools.shutil.which", return_value="/usr/local/bin/subfinder"), \
             patch("osint_tools._run_command", side_effect=subprocess.TimeoutExpired("subfinder", 60)):
            result = osint_tools.subdomain_enum("example.com")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "timeout")

    def test_normalizes_domain(self):
        with patch("osint_tools.shutil.which", return_value="/usr/local/bin/subfinder"), \
             patch("osint_tools._run_command", return_value=(0, "", "")) as mock_run:
            osint_tools.subdomain_enum("https://www.example.com/path")
        argv = mock_run.call_args[0][0]
        self.assertIn("example.com", argv)

    def test_max_results_cap(self):
        lines = "\n".join(
            json.dumps({"host": f"sub{i}.example.com", "source": "x", "ip": ""})
            for i in range(20)
        )
        with patch("osint_tools.shutil.which", return_value="/usr/local/bin/subfinder"), \
             patch("osint_tools._run_command", return_value=(0, lines, "")):
            result = osint_tools.subdomain_enum("example.com", max_results=5)
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 20)
        self.assertEqual(len(result["subdomains"]), 5)


# ---------------------------------------------------------------------------
# WHOIS lookup
# ---------------------------------------------------------------------------

SAMPLE_WHOIS = """\
Domain Name: GOOGLE.COM
Registrar: MarkMonitor Inc.
Creation Date: 1997-09-15T04:00:00Z
Registry Expiry Date: 2028-09-14T04:00:00Z
Name Server: NS1.GOOGLE.COM
Name Server: NS2.GOOGLE.COM
Domain Status: clientDeleteProhibited https://icann.org/epp#clientDeleteProhibited
Domain Status: clientTransferProhibited https://icann.org/epp#clientTransferProhibited
Registrant Organization: Google LLC
"""


class OsintWhoisLookupTests(unittest.TestCase):
    def test_rejects_invalid_domain(self):
        result = osint_tools.whois_lookup("not a domain!")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_domain")

    def test_reports_missing_tool(self):
        with patch("osint_tools.shutil.which", return_value=None):
            result = osint_tools.whois_lookup("google.com")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "tool_missing")

    def test_parses_registrar(self):
        with patch("osint_tools.shutil.which", return_value="/usr/bin/whois"), \
             patch("osint_tools._run_command", return_value=(0, SAMPLE_WHOIS, "")):
            result = osint_tools.whois_lookup("google.com")
        self.assertTrue(result["ok"])
        self.assertEqual(result["registrar"], "MarkMonitor Inc.")

    def test_parses_created_and_expires(self):
        with patch("osint_tools.shutil.which", return_value="/usr/bin/whois"), \
             patch("osint_tools._run_command", return_value=(0, SAMPLE_WHOIS, "")):
            result = osint_tools.whois_lookup("google.com")
        self.assertEqual(result["created"], "1997-09-15T04:00:00Z")
        self.assertEqual(result["expires"], "2028-09-14T04:00:00Z")

    def test_parses_name_servers(self):
        with patch("osint_tools.shutil.which", return_value="/usr/bin/whois"), \
             patch("osint_tools._run_command", return_value=(0, SAMPLE_WHOIS, "")):
            result = osint_tools.whois_lookup("google.com")
        self.assertIn("NS1.GOOGLE.COM", result["name_servers"])
        self.assertIn("NS2.GOOGLE.COM", result["name_servers"])

    def test_parses_domain_status(self):
        with patch("osint_tools.shutil.which", return_value="/usr/bin/whois"), \
             patch("osint_tools._run_command", return_value=(0, SAMPLE_WHOIS, "")):
            result = osint_tools.whois_lookup("google.com")
        self.assertIn("clientDeleteProhibited", result["status"])

    def test_parses_registrant_org(self):
        with patch("osint_tools.shutil.which", return_value="/usr/bin/whois"), \
             patch("osint_tools._run_command", return_value=(0, SAMPLE_WHOIS, "")):
            result = osint_tools.whois_lookup("google.com")
        self.assertEqual(result["registrant_org"], "Google LLC")

    def test_raw_truncated_present(self):
        with patch("osint_tools.shutil.which", return_value="/usr/bin/whois"), \
             patch("osint_tools._run_command", return_value=(0, SAMPLE_WHOIS, "")):
            result = osint_tools.whois_lookup("google.com")
        self.assertIn("raw_truncated", result)
        self.assertLessEqual(len(result["raw_truncated"]), 800)

    def test_timeout_returns_error(self):
        import subprocess
        with patch("osint_tools.shutil.which", return_value="/usr/bin/whois"), \
             patch("osint_tools._run_command", side_effect=subprocess.TimeoutExpired("whois", 15)):
            result = osint_tools.whois_lookup("google.com")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "timeout")

    def test_normalizes_domain_with_www(self):
        with patch("osint_tools.shutil.which", return_value="/usr/bin/whois"), \
             patch("osint_tools._run_command", return_value=(0, SAMPLE_WHOIS, "")) as mock_run:
            osint_tools.whois_lookup("www.google.com")
        argv = mock_run.call_args[0][0]
        self.assertEqual(argv[-1], "google.com")


# ---------------------------------------------------------------------------
# _ScanCache
# ---------------------------------------------------------------------------

class ScanCacheTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cache = _make_isolated_cache(Path(self.td.name))

    def tearDown(self):
        self.td.cleanup()

    def test_put_and_get_returns_stored_result(self):
        self.cache.put("maigret", "aman", {"profiles": [{"site": "github"}]})
        result = self.cache.get("maigret", "aman")
        self.assertIsNotNone(result)
        self.assertEqual(result["profiles"][0]["site"], "github")

    def test_get_returns_none_for_missing_key(self):
        self.assertIsNone(self.cache.get("maigret", "nobody"))

    def test_get_returns_none_after_ttl_expiry(self):
        with patch.dict(osint_tools._CACHE_TTL, {"maigret": 0}):
            self.cache.put("maigret", "aman", {"profiles": []})
            time.sleep(0.05)
            result = self.cache.get("maigret", "aman")
        self.assertIsNone(result)

    def test_clear_all_empties_table(self):
        self.cache.put("maigret", "aman", {"profiles": []})
        self.cache.put("dnstwist", "example.com", {"candidates": []})
        self.cache.clear()
        self.assertIsNone(self.cache.get("maigret", "aman"))
        self.assertIsNone(self.cache.get("dnstwist", "example.com"))

    def test_clear_by_tool_only_removes_that_tool(self):
        self.cache.put("maigret", "aman", {"profiles": []})
        self.cache.put("dnstwist", "example.com", {"candidates": []})
        self.cache.clear(tool="maigret")
        self.assertIsNone(self.cache.get("maigret", "aman"))
        self.assertIsNotNone(self.cache.get("dnstwist", "example.com"))

    def test_clear_by_target_only_removes_that_target(self):
        self.cache.put("maigret", "aman", {"profiles": []})
        self.cache.put("maigret", "bob", {"profiles": []})
        self.cache.clear(target="aman")
        self.assertIsNone(self.cache.get("maigret", "aman"))
        self.assertIsNotNone(self.cache.get("maigret", "bob"))

    def test_list_entries_returns_stored_metadata(self):
        self.cache.put("maigret", "aman", {"profiles": []})
        entries = self.cache.list_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["tool"], "maigret")
        self.assertEqual(entries[0]["target"], "aman")
        self.assertIn("cached_at", entries[0])

    def test_put_replaces_existing_entry(self):
        self.cache.put("maigret", "aman", {"profiles": [{"site": "github"}]})
        self.cache.put("maigret", "aman", {"profiles": [{"site": "twitter"}]})
        result = self.cache.get("maigret", "aman")
        self.assertEqual(result["profiles"][0]["site"], "twitter")

    def test_thread_safe_concurrent_writes(self):
        errors = []

        def write(i):
            try:
                self.cache.put("maigret", f"user{i}", {"profiles": []})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(self.cache.list_entries()), 20)


# ---------------------------------------------------------------------------
# Domain / username normalization
# ---------------------------------------------------------------------------

class NormalizationTests(unittest.TestCase):
    def test_strips_scheme_and_path_from_domain(self):
        self.assertEqual(osint_tools._normalize_domain("https://www.example.com/path"), "example.com")

    def test_strips_www_prefix(self):
        self.assertEqual(osint_tools._normalize_domain("www.example.com"), "example.com")

    def test_rejects_domain_with_spaces(self):
        self.assertEqual(osint_tools._normalize_domain("bad domain.com"), "")

    def test_rejects_empty_domain(self):
        self.assertEqual(osint_tools._normalize_domain(""), "")

    def test_valid_username_passes(self):
        self.assertEqual(osint_tools._normalize_username("aman_imran"), "aman_imran")

    def test_username_with_space_rejected(self):
        self.assertEqual(osint_tools._normalize_username("bad user"), "")

    def test_username_over_64_chars_rejected(self):
        self.assertEqual(osint_tools._normalize_username("a" * 65), "")


if __name__ == "__main__":
    unittest.main()
