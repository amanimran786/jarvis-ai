"""Tests for task_planner — focusing on replan_after_failure and _extract_json_steps."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from task_planner import (
    TaskStep,
    _build_steps,
    _extract_json_steps,
    _local_planner_options,
    _plan_task_local,
    replan_after_failure,
)


def _ollama_response(content: str, thinking: str = "") -> object:
    """Minimal mock that looks like ollama.Client.chat() return value."""
    return SimpleNamespace(message=SimpleNamespace(content=content, thinking=thinking))


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

    def test_caps_untrusted_plan_at_requested_limit(self):
        data = [
            {"number": index, "description": "step", "tool": "chat", "params": {}}
            for index in range(1, 6)
        ]
        steps = _build_steps(data, max_steps=3)
        self.assertEqual(len(steps), 3)

    def test_rejects_non_object_params(self):
        data = [{"number": 1, "description": "step", "tool": "chat", "params": "bad"}]
        with self.assertRaisesRegex(ValueError, "params must be an object"):
            _build_steps(data)


# ── _plan_task_local ──────────────────────────────────────────────────────────

class LocalPlannerRequestTests(unittest.TestCase):
    def test_unknown_fallback_model_uses_conservative_context(self):
        with patch("brains.brain_ollama._ollama_options_for_model", return_value={}):
            options = _local_planner_options("unknown-local-model")

        self.assertEqual(options["num_ctx"], 8_192)

    def test_rejects_non_positive_model_limits_before_request(self):
        with patch(
            "brains.brain_ollama._ollama_options_for_model",
            return_value={"num_ctx": 0, "num_predict": 1_024},
        ):
            with self.assertRaisesRegex(ValueError, "must be positive"):
                _local_planner_options("misconfigured-model")

    def test_disables_thinking_and_bounds_generation(self):
        raw = '{"steps": [{"number": 1, "description": "plan", "tool": "chat", "params": {}}]}'
        with patch("ollama.Client.chat", return_value=_ollama_response(raw, thinking="ignored")) as chat, \
             patch("brains.brain_ollama.get_best_available", return_value="qwen3:30b-a3b"), \
             patch("brains.brain_ollama._ollama_options_for_model", return_value={"num_ctx": 131_072}):
            steps = _plan_task_local("do the task")

        request = chat.call_args.kwargs
        self.assertEqual(len(steps), 1)
        self.assertIs(request["think"], False)
        self.assertEqual(request["options"]["num_ctx"], 32_768)
        self.assertEqual(request["options"]["num_predict"], 1_024)
        self.assertEqual(request["options"]["temperature"], 0)
        self.assertEqual(request["format"]["properties"]["steps"]["maxItems"], 12)

    def test_caps_untrusted_task_text_before_request(self):
        raw = '{"steps": [{"number": 1, "description": "plan", "tool": "chat", "params": {}}]}'
        with patch("ollama.Client.chat", return_value=_ollama_response(raw)) as chat, \
             patch("brains.brain_ollama.get_best_available", return_value="qwen3:30b-a3b"), \
             patch("brains.brain_ollama._ollama_options_for_model", return_value={"num_ctx": 32_768}):
            _plan_task_local("x" * 20_000)

        prompt = chat.call_args.kwargs["messages"][-1]["content"]
        self.assertEqual(len(prompt), len("Plan this task: ") + 12_000)


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
        resp = _ollama_response(raw)
        return patch("ollama.Client.chat", return_value=resp), \
               patch("brains.brain_ollama.get_best_available", return_value="qwen3:30b-a3b")

    def test_returns_corrective_steps_on_success(self):
        raw = '{"steps": [{"number": 1, "description": "retry write", "tool": "chat", "params": {}}]}'
        with patch("ollama.Client.chat", return_value=_ollama_response(raw)) as chat, \
             patch("brains.brain_ollama.get_best_available", return_value="qwen3:30b-a3b"), \
             patch("brains.brain_ollama._ollama_options_for_model", return_value={"num_ctx": 131_072}):
            result = replan_after_failure(
                "do the task", self._completed(), self._failed_step(), "Permission denied"
            )
        request = chat.call_args.kwargs
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 1)
        self.assertIs(request["think"], False)
        self.assertEqual(request["options"]["num_ctx"], 32_768)
        self.assertEqual(request["options"]["num_predict"], 1_024)
        self.assertEqual(request["format"]["properties"]["steps"]["maxItems"], 3)

    def test_corrective_step_numbers_offset_after_failed(self):
        raw = '{"steps": [{"number": 1, "description": "fix", "tool": "chat", "params": {}}]}'
        with patch("ollama.Client.chat", return_value=_ollama_response(raw)), \
             patch("brains.brain_ollama.get_best_available", return_value="qwen3:30b-a3b"):
            result = replan_after_failure(
                "task", self._completed(), self._failed_step(), "error"
            )
        # failed_step.number=2, so corrective should start at 3
        self.assertEqual(result[0].number, 3)

    def test_returns_none_when_local_fails(self):
        with patch("ollama.Client.chat", side_effect=RuntimeError("model down")), \
             patch("brains.brain_ollama.get_best_available", return_value="qwen3:30b-a3b"):
            result = replan_after_failure(
                "task", self._completed(), self._failed_step(), "error"
            )
        self.assertIsNone(result)

    def test_returns_none_on_unparseable_output(self):
        with patch("ollama.Client.chat", return_value=_ollama_response("I cannot help with that.")), \
             patch("brains.brain_ollama.get_best_available", return_value="qwen3:30b-a3b"):
            result = replan_after_failure(
                "task", self._completed(), self._failed_step(), "error"
            )
        self.assertIsNone(result)

    def test_handles_empty_completed_steps(self):
        raw = '{"steps": [{"number": 1, "description": "start over", "tool": "chat", "params": {}}]}'
        with patch("ollama.Client.chat", return_value=_ollama_response(raw)), \
             patch("brains.brain_ollama.get_best_available", return_value="qwen3:30b-a3b"):
            result = replan_after_failure("task", [], self._failed_step(), "error")
        self.assertIsNotNone(result)

    def test_multiple_corrective_steps_renumbered(self):
        raw = '{"steps": [{"number": 1, "description": "A", "tool": "chat", "params": {}}, {"number": 2, "description": "B", "tool": "chat", "params": {}}]}'
        with patch("ollama.Client.chat", return_value=_ollama_response(raw)), \
             patch("brains.brain_ollama.get_best_available", return_value="qwen3:30b-a3b"):
            result = replan_after_failure(
                "task", self._completed(), self._failed_step(), "error"
            )
        self.assertEqual(result[0].number, 3)
        self.assertEqual(result[1].number, 4)


if __name__ == "__main__":
    unittest.main()
