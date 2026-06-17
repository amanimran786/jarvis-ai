"""Tests for skill_monitor — usage tracking and integration inventory."""

import importlib
import sys
import threading
import time
import unittest


def _fresh_module():
    """Import skill_monitor with a clean stats state."""
    name = "skill_monitor"
    if name in sys.modules:
        del sys.modules[name]
    return importlib.import_module(name)


class SkillMonitorUsageTests(unittest.TestCase):
    def setUp(self):
        self.sm = _fresh_module()

    def test_record_call_increments_total(self):
        self.sm.record_call("Calendar", True, 42.0)
        stats = self.sm.get_stats()
        self.assertEqual(stats["Calendar"]["total_calls"], 1)

    def test_record_call_tracks_success_and_failure(self):
        self.sm.record_call("Browser", True, 10.0)
        self.sm.record_call("Browser", False, 5.0, error="timeout")
        stats = self.sm.get_stats()["Browser"]
        self.assertEqual(stats["success"], 1)
        self.assertEqual(stats["failure"], 1)

    def test_record_call_computes_avg_latency(self):
        self.sm.record_call("Search", True, 100.0)
        self.sm.record_call("Search", True, 200.0)
        stats = self.sm.get_stats()["Search"]
        self.assertAlmostEqual(stats["avg_latency_ms"], 150.0, places=0)

    def test_record_call_stores_last_error(self):
        self.sm.record_call("Ollama", False, 99.0, error="connection refused")
        self.assertEqual(self.sm.get_stats()["Ollama"]["last_error"], "connection refused")

    def test_record_call_noop_on_empty_skill(self):
        self.sm.record_call("", True, 10.0)
        self.assertEqual(self.sm.get_stats(), {})

    def test_get_recent_log_returns_entries(self):
        for i in range(5):
            self.sm.record_call(f"skill_{i}", True, float(i * 10))
        log = self.sm.get_recent_log(3)
        self.assertEqual(len(log), 3)
        self.assertEqual(log[-1]["skill"], "skill_4")

    def test_recent_log_capped_at_200(self):
        for i in range(250):
            self.sm.record_call("flood", True, 1.0)
        log = self.sm.get_recent_log(200)
        self.assertEqual(len(log), 200)

    def test_thread_safety(self):
        errors = []

        def worker(name):
            try:
                for _ in range(50):
                    self.sm.record_call(name, True, 1.0)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        total = sum(s["total_calls"] for s in self.sm.get_stats().values())
        self.assertEqual(total, 500)


class SkillMonitorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.sm = _fresh_module()

    def test_osint_tools_returned(self):
        status = self.sm.integration_status()
        for name in ("maigret", "dnstwist", "subfinder", "whois"):
            self.assertIn(name, status, f"OSINT tool {name!r} missing from integration_status")
            entry = status[name]
            self.assertEqual(entry["category"], "osint")
            self.assertIn("available", entry)

    def test_cloud_llm_providers_returned(self):
        status = self.sm.integration_status()
        for name in ("claude", "gemini", "kimi"):
            self.assertIn(name, status, f"LLM provider {name!r} missing")
            self.assertEqual(status[name]["category"], "cloud_llm")

    def test_system_integrations_returned(self):
        status = self.sm.integration_status()
        for name in ("browser", "meeting_listener", "camera", "hardware"):
            self.assertIn(name, status)

    def test_integration_status_cached(self):
        t1 = time.monotonic()
        self.sm.integration_status(force=True)
        t2 = time.monotonic()
        self.sm.integration_status()
        t3 = time.monotonic()
        # Second call should be much faster (cache hit vs cold check)
        self.assertLess(t3 - t2, (t2 - t1) + 0.5)

    def test_get_report_structure(self):
        report = self.sm.get_report()
        for key in ("health", "integrations", "skills", "agents", "summary", "recent_log"):
            self.assertIn(key, report)
        summary = report["summary"]
        self.assertIn("total_skill_calls", summary)
        self.assertIn("integrations_available", summary)
        self.assertIn("integrations_unavailable", summary)

    def test_get_report_skills_includes_tool_registry(self):
        report = self.sm.get_report()
        # tool_registry.TOOLS has at least these
        for expected in ("chat", "search", "calendar", "email"):
            self.assertIn(expected, report["skills"])

    def test_get_report_agents_includes_roster(self):
        report = self.sm.get_report()
        # config.AGENT_ROSTER has at least one agent
        self.assertGreater(len(report["agents"]), 0)
        first_agent = next(iter(report["agents"].values()))
        self.assertIn("role", first_agent)


if __name__ == "__main__":
    unittest.main()
