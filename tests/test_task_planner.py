"""Tests for task_planner — focusing on replan_after_failure and _extract_json_steps."""
import unittest
from unittest.mock import patch

from task_planner import (
    TaskStep,
    _extract_json_steps,
    _build_steps,
    replan_after_failure,
)


# ── _extract_json_steps ───────────────────────────────────────────────────────

class ExtractJsonStepsTests(unittest.TestCase):
    def test_bare_array(self):
        raw = '[{"number": 1, "description": "do it", "tool": "chat", "params": {}}]'
        steps = _extract_json_steps(raw)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["tool"], "chat")

    def test_wrapped_object(self):
        raw = '{"steps": [{"number": 1, "description": "do it", "tool": "chat", "params": {}}]}'
        steps = _extract_json_steps(raw)
        self.assertEqual(len(steps), 1)

    def test_markdown_code_fence_stripped(self):
        raw = '```json\n{"steps": [{"number": 1, "description": "x", "tool": "chat", "params": {}}]}\n```'
        steps = _extract_json_steps(raw)
        self.assertEqual(len(steps), 1)

    def test_raises_on_no_json(self):
        with self.assertRaises(ValueError):
            _extract_json_steps("Sorry, I cannot do that.")

    def test_two_steps_parsed(self):
        raw = '[{"number": 1, "description": "A", "tool": "chat", "params": {}}, {"number": 2, "description": "B", "tool": "shell", "params": {"cmd": "ls"}}]'
        steps = _extract_json_steps(raw)
        self.assertEqual(len(steps), 2)


# ── _build_steps ──────────────────────────────────────────────────────────────

class BuildStepsTests(unittest.TestCase):
    def _data(self):
        return [
            {"number": 1, "description": "first", "tool": "chat", "params": {}},
            {"number": 2, "description": "second", "tool": "nonexistent_tool_xyz", "params": {}},
        ]

    def test_returns_task_steps(self):
        steps = _build_steps(self._data())
        self.assertIsInstance(steps[0], TaskStep)

    def test_unknown_tool_falls_back_to_chat(self):
        steps = _build_steps(self._data())
        self.assertEqual(steps[1].tool, "chat")

    def test_step_numbering_preserved(self):
        steps = _build_steps(self._data())
        self.assertEqual(steps[0].number, 1)
        self.assertEqual(steps[1].number, 2)


# ── replan_after_failure ──────────────────────────────────────────────────────

class ReplanAfterFailureTests(unittest.TestCase):
    def _failed_step(self):
        return TaskStep(number=2, description="write file", tool="write_file",
                        params={"path": "/x.txt", "content": "data"})

    def _completed(self):
        s = TaskStep(number=1, description="read config", tool="chat", params={})
        s.ok = True
        return [s]

    def _mock_local(self, raw: str):
        return patch("brains.brain_ollama.ask_local", return_value=raw)

    def test_returns_corrective_steps_on_success(self):
        raw = '{"steps": [{"number": 1, "description": "retry write", "tool": "chat", "params": {}}]}'
        with self._mock_local(raw):
            result = replan_after_failure(
                "do the task", self._completed(), self._failed_step(), "Permission denied"
            )
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 1)

    def test_corrective_step_numbers_offset_after_failed(self):
        raw = '{"steps": [{"number": 1, "description": "fix", "tool": "chat", "params": {}}]}'
        with self._mock_local(raw):
            result = replan_after_failure(
                "task", self._completed(), self._failed_step(), "error"
            )
        # failed_step.number=2, so corrective should start at 3
        self.assertEqual(result[0].number, 3)

    def test_returns_none_when_local_fails(self):
        with patch("brains.brain_ollama.ask_local", side_effect=RuntimeError("model down")):
            result = replan_after_failure(
                "task", self._completed(), self._failed_step(), "error"
            )
        self.assertIsNone(result)

    def test_returns_none_on_unparseable_output(self):
        with self._mock_local("I cannot help with that."):
            result = replan_after_failure(
                "task", self._completed(), self._failed_step(), "error"
            )
        self.assertIsNone(result)

    def test_handles_empty_completed_steps(self):
        raw = '{"steps": [{"number": 1, "description": "start over", "tool": "chat", "params": {}}]}'
        with self._mock_local(raw):
            result = replan_after_failure("task", [], self._failed_step(), "error")
        self.assertIsNotNone(result)

    def test_multiple_corrective_steps_renumbered(self):
        raw = '{"steps": [{"number": 1, "description": "A", "tool": "chat", "params": {}}, {"number": 2, "description": "B", "tool": "chat", "params": {}}]}'
        with self._mock_local(raw):
            result = replan_after_failure(
                "task", self._completed(), self._failed_step(), "error"
            )
        self.assertEqual(result[0].number, 3)
        self.assertEqual(result[1].number, 4)


if __name__ == "__main__":
    unittest.main()
