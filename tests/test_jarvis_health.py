"""
Unit tests for jarvis_health.py — component health monitor.

Tests cover:
  - check_all returns all expected component keys
  - degraded() returns a list
  - spoken_summary produces a non-empty string
  - health_summary produces a non-empty string
  - cache TTL behaviour
  - individual checker graceful error handling
"""

import sys
import os
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import jarvis_health as jh


class CheckAllStructureTests(unittest.TestCase):
    def setUp(self):
        jh._cache = {}
        jh._cache_at = 0.0

    def test_returns_all_expected_keys(self):
        """check_all must include every registered component."""
        # Patch all checkers to return instantly to avoid real service calls
        fast_ok = {"name": "x", "ok": True, "detail": "ok", "degraded": False}
        patches = {name: (lambda n=name: {**fast_ok, "name": n})
                   for name in jh._CHECKERS}
        with patch.dict(jh._CHECKERS, patches, clear=True):
            # Force re-check ignoring cache
            result = jh.check_all(force=True)
        for key in ("ollama", "stt", "tts", "google", "mem0", "vault", "watcher"):
            self.assertIn(key, result)

    def test_each_status_has_required_fields(self):
        fast_ok = {"name": "x", "ok": True, "detail": "ok", "degraded": False}
        # Patch every checker
        with patch.dict(
            jh._CHECKERS,
            {name: (lambda n=name: {**fast_ok, "name": n}) for name in jh._CHECKERS},
            clear=True,
        ):
            result = jh.check_all(force=True)
        for name, status in result.items():
            self.assertIn("ok", status)
            self.assertIn("detail", status)
            self.assertIn("degraded", status)

    def test_slow_checker_is_reported_as_timed_out(self):
        def fast():
            return {"name": "fast", "ok": True, "detail": "ok", "degraded": False}

        def slow():
            time.sleep(0.2)
            return {"name": "slow", "ok": True, "detail": "late", "degraded": False}

        with patch.dict(jh._CHECKERS, {"fast": fast, "slow": slow}, clear=True), \
             patch("jarvis_health._CHECK_TIMEOUT_SECONDS", 0.01):
            result = jh.check_all(force=True)

        self.assertTrue(result["fast"]["ok"])
        self.assertFalse(result["slow"]["ok"])
        self.assertTrue(result["slow"]["degraded"])
        self.assertIn("timed out", result["slow"]["detail"].lower())


class DegradedTests(unittest.TestCase):
    def test_degraded_returns_list(self):
        result = jh.degraded()
        self.assertIsInstance(result, list)

    def test_degraded_contains_only_failing_names(self):
        # Inject a known bad component into cache
        jh._cache = {
            "ollama": {"name": "ollama", "ok": True,  "detail": "ok", "degraded": False},
            "stt":    {"name": "stt",    "ok": False, "detail": "down", "degraded": True},
        }
        jh._cache_at = time.monotonic()  # mark cache as fresh
        result = jh.degraded()
        self.assertIn("stt", result)
        self.assertNotIn("ollama", result)


class SummaryTests(unittest.TestCase):
    def test_spoken_summary_all_ok(self):
        jh._cache = {
            name: {"name": name, "ok": True, "detail": "ok", "degraded": False}
            for name in jh._CHECKERS
        }
        jh._cache_at = time.monotonic()
        summary = jh.spoken_summary()
        self.assertIn("operational", summary.lower())

    def test_spoken_summary_one_degraded(self):
        jh._cache = {
            "ollama": {"name": "ollama", "ok": False, "detail": "not running", "degraded": True},
            "stt":    {"name": "stt",    "ok": True,  "detail": "ok", "degraded": False},
        }
        jh._cache_at = time.monotonic()
        summary = jh.spoken_summary()
        self.assertIn("degraded", summary.lower())

    def test_health_summary_non_empty(self):
        jh._cache = {
            "vault": {"name": "vault", "ok": True, "detail": "200 docs", "degraded": False},
        }
        jh._cache_at = time.monotonic()
        summary = jh.health_summary()
        self.assertTrue(len(summary.strip()) > 0)


class IndividualCheckerTests(unittest.TestCase):
    def test_check_ollama_graceful_when_offline(self):
        """_check_ollama must return a ComponentStatus dict even when Ollama is down."""
        with patch("brains.brain_ollama.list_local_models", side_effect=ConnectionRefusedError):
            result = jh._check_ollama()
        self.assertIn("ok", result)
        self.assertFalse(result["ok"])
        self.assertTrue(result["degraded"])

    def test_check_mem0_graceful_when_unavailable(self):
        with patch("mem0_layer.status", return_value={"available": False}):
            result = jh._check_mem0()
        self.assertFalse(result["ok"])

    def test_check_watcher_reports_running(self):
        mock_status = {
            "enabled": True,
            "running": True,
            "morning_brief_sent": "2026-04-24",
        }
        with patch("jarvis_watcher.status", return_value=mock_status):
            result = jh._check_watcher()
        self.assertTrue(result["ok"])
        self.assertFalse(result["degraded"])


if __name__ == "__main__":
    unittest.main()
