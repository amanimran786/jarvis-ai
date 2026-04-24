"""
Unit tests for jarvis_executor.py — multi-step task executor.

Tests cover:
  - heuristic step splitter (no LLM)
  - is_multi_step detection
  - execute_step graceful error handling
  - synthesise_results fallback path
  - run() with mocked route_stream
"""

import sys
import os
import unittest
from unittest.mock import MagicMock

# ── Environment bootstrap ─────────────────────────────────────────────────────
# executor imports `router` lazily (inside execute_step) and `model_router`
# lazily (inside parse_steps / synthesise_results).  Both pull in PyQt6 /
# Ollama when the real modules are loaded, which is unavailable in CI.
# Inject lightweight mocks so the lazy imports succeed without real services.

if "router" not in sys.modules:
    sys.modules["router"] = MagicMock()

if "model_router" not in sys.modules:
    _mr_mock = MagicMock()
    # smart_stream fallback: raise ImportError so tests exercise fallback path
    _mr_mock.smart_stream.side_effect = ImportError("no model_router in CI")
    sys.modules["model_router"] = _mr_mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import jarvis_executor as je


# ── Helpers ───────────────────────────────────────────────────────────────────

def _set_route_stream(return_value=None, side_effect=None):
    """Point sys.modules['router'].route_stream at a fresh mock for this test."""
    m = MagicMock()
    if side_effect is not None:
        m.side_effect = side_effect
    else:
        m.return_value = return_value
    sys.modules["router"].route_stream = m
    return m


# ── Tests ─────────────────────────────────────────────────────────────────────

class HeuristicSplitTests(unittest.TestCase):
    def test_simple_and_also_splits(self):
        # Both parts must have ≥ _MIN_STEP_WORDS (3) words to survive the filter
        steps = je._heuristic_split(
            "message dad tonight and also add a task to call him tomorrow"
        )
        self.assertEqual(len(steps), 2)

    def test_then_splits(self):
        steps = je._heuristic_split(
            "set a timer for 10 minutes then remind me to drink water"
        )
        self.assertGreaterEqual(len(steps), 2)

    def test_single_action_not_split(self):
        steps = je._heuristic_split("what's the weather today")
        self.assertEqual(len(steps), 1)

    def test_short_fragments_not_kept(self):
        """Parts with fewer than 3 words should be dropped."""
        steps = je._heuristic_split("do this and hi and message dad about dinner tonight")
        for step in steps:
            self.assertGreaterEqual(len(step.split()), je._MIN_STEP_WORDS)

    def test_and_additionally_splits(self):
        steps = je._heuristic_split(
            "create a calendar event additionally message fiza about the meeting time"
        )
        self.assertGreaterEqual(len(steps), 2)


class MultiStepDetectionTests(unittest.TestCase):
    def test_and_also_detected(self):
        self.assertTrue(je.is_multi_step("message dad and also remind me to call him"))

    def test_and_then_detected(self):
        self.assertTrue(je.is_multi_step("add a task and then message mom"))

    def test_after_that_detected(self):
        self.assertTrue(je.is_multi_step("send the email after that add it to the vault"))

    def test_simple_query_not_multi(self):
        self.assertFalse(je.is_multi_step("what time is my meeting"))

    def test_single_and_not_multi(self):
        """'and' alone between two nouns should not trigger multi-step."""
        self.assertFalse(je.is_multi_step("message mom and dad"))


class ExecuteStepTests(unittest.TestCase):
    def test_successful_step(self):
        """A step that routes cleanly should return ok=True."""
        _set_route_stream(return_value=(iter(["Done.", " Task complete."]), "Test"))
        result = je.execute_step("set a reminder for 5pm")
        self.assertTrue(result["ok"])
        self.assertIn("Done", result["output"])

    def test_empty_stream_returns_error(self):
        """An empty stream should return ok=False."""
        _set_route_stream(return_value=(iter([]), "Test"))
        result = je.execute_step("something that returns nothing")
        self.assertFalse(result["ok"])

    def test_exception_in_route_stream_handled(self):
        """route_stream raising should return ok=False, not crash."""
        _set_route_stream(side_effect=RuntimeError("boom"))
        result = je.execute_step("this will fail")
        self.assertFalse(result["ok"])
        self.assertIn("Error", result["output"])


class SynthesiseResultsTests(unittest.TestCase):
    def test_fallback_path_ok_steps(self):
        """When LLM unavailable, fallback produces a readable summary."""
        results = [
            {"step": "Message dad", "ok": True, "output": "Message sent."},
            {"step": "Add task",    "ok": True, "output": "Task added."},
        ]
        # model_router mock already raises ImportError; synthesise_results
        # should fall through to the concatenation fallback
        summary = je.synthesise_results("message dad and add task", results)
        self.assertIn("Message dad", summary)

    def test_fallback_flags_failures(self):
        results = [
            {"step": "Message dad",    "ok": True,  "output": "Sent."},
            {"step": "Check calendar", "ok": False, "output": "Error: timeout"},
        ]
        summary = je.synthesise_results("message dad and check calendar", results)
        self.assertIn("Check calendar", summary)

    def test_empty_results_handled(self):
        summary = je.synthesise_results("do something", [])
        self.assertIsInstance(summary, str)
        self.assertTrue(len(summary) > 0)


class ParseStepsTests(unittest.TestCase):
    def test_compound_request_splits_without_llm(self):
        """Heuristic should handle standard compound requests (both parts ≥3 words)."""
        steps = je.parse_steps("message dad tonight and also set a 5 minute timer")
        self.assertGreaterEqual(len(steps), 2)

    def test_single_action_returns_one_step(self):
        steps = je.parse_steps("what's the weather in San Jose")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0], "what's the weather in San Jose")

    def test_empty_goal_handled(self):
        result = je.run("")
        self.assertIsInstance(result, str)


if __name__ == "__main__":
    unittest.main()
