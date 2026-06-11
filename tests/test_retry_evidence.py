"""Retry evidence carry-over + empty-result fallback in the tool loop."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import task_runtime


def _capture_verify_prompt(**kwargs):
    captured = {}

    def fake_ask_stream(prompt, **_kw):
        captured["prompt"] = prompt
        return iter(['{"score": 1.0, "reason": "ok"}'])

    with patch("brains.brain.ask_stream", fake_ask_stream):
        verdict = task_runtime._auto_verify(
            "task_x", "qa-tester", "count files", "There are 77 files.", **kwargs
        )
    return captured["prompt"], verdict


class InheritedEvidenceVerifierTests(unittest.TestCase):
    def test_zero_calls_without_evidence_gets_fabrication_clause(self):
        prompt, _ = _capture_verify_prompt(tool_calls=0)
        self.assertIn("ZERO tool calls", prompt)
        self.assertIn("set the overall score to 0.0", prompt)

    def test_zero_calls_with_inherited_evidence_gets_grounded_clause(self):
        prompt, _ = _capture_verify_prompt(
            tool_calls=0, inherited_transcript="$ find tests/ -name *.py | wc -l\n77"
        )
        self.assertIn("carried over REAL tool outputs", prompt)
        self.assertIn("must NOT be treated as fabricated", prompt)
        self.assertIn("77", prompt)
        self.assertNotIn("set the overall score to 0.0", prompt)

    def test_own_tool_calls_still_use_own_transcript(self):
        prompt, _ = _capture_verify_prompt(
            tool_calls=2, tool_transcript="$ ls\nfoo.py",
            inherited_transcript="stale prior evidence",
        )
        self.assertIn("foo.py", prompt)
        self.assertNotIn("stale prior evidence", prompt)


class EmptyResultFallbackTests(unittest.TestCase):
    def setUp(self):
        task_runtime.reset_for_tests()

    def test_empty_continuation_falls_back_to_tool_outputs(self):
        # Initial response calls a tool; continuation streams zero chunks.
        with patch.object(task_runtime, "smart_stream",
                          return_value=(iter([]), "UnitTestModel")), \
             patch.object(task_runtime, "_tool_bash", return_value="77"):
            response, _model, _idx, calls, transcript = task_runtime._run_tool_loop(
                "task_empty", "count files", "",
                "<bash>find tests/ -name *.py | wc -l</bash>", "UnitTestModel", 0,
            )
        self.assertEqual(calls, 1)
        self.assertTrue(response.strip())
        self.assertIn("77", response)
        self.assertIn("runtime note", response)
        self.assertIn("77", transcript)

    def test_no_tools_empty_response_gets_placeholder(self):
        response, _m, _i, calls, _t = task_runtime._run_tool_loop(
            "task_empty2", "say hi", "", "   ", "UnitTestModel", 0,
        )
        self.assertEqual(calls, 0)
        self.assertIn("returned no text", response)


if __name__ == "__main__":
    unittest.main()
