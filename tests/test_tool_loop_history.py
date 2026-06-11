"""Tool-loop continuation prompts must not grow unbounded with history."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import task_runtime


class ToolLoopHistoryCapTests(unittest.TestCase):
    def setUp(self):
        task_runtime.reset_for_tests()

    def _run_with_big_outputs(self, output_size: int):
        prompts: list[str] = []

        def fake_smart_stream(prompt, **_kw):
            prompts.append(prompt)
            if len(prompts) < 3:
                return iter(["checking again <bash>echo more</bash>"]), "M"
            return iter(["final answer"]), "M"

        with patch.object(task_runtime, "smart_stream", side_effect=fake_smart_stream), \
             patch.object(task_runtime, "_tool_bash", return_value="x" * output_size):
            response, _m, _i, calls, _t = task_runtime._run_tool_loop(
                "task_hist", "analyze", "", "<bash>echo start</bash>", "M", 0,
            )
        return response, calls, prompts

    def test_older_steps_are_truncated(self):
        response, calls, prompts = self._run_with_big_outputs(5000)
        self.assertEqual(response, "final answer")
        self.assertEqual(calls, 3)
        # Round 3's prompt contains step-1 history as an OLD step → hard cap
        self.assertIn("[...truncated]", prompts[-1])

    def test_prompt_size_is_bounded(self):
        _, _, prompts = self._run_with_big_outputs(20000)
        # Without compaction round 3 would carry 2 full 20KB outputs (~40KB+).
        self.assertLess(max(len(p) for p in prompts), 16000)

    def test_small_histories_pass_through_untouched(self):
        _, _, prompts = self._run_with_big_outputs(50)
        self.assertNotIn("[...truncated]", prompts[-1])
        self.assertNotIn("elided", prompts[-1])


if __name__ == "__main__":
    unittest.main()
