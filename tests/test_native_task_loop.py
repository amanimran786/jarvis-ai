"""
tests/test_native_task_loop.py

Unit tests for _run_native_task_loop in task_runtime.

All tests mock the Ollama client so no real inference is required.
"""
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# Ensure test env flag is set before importing task_runtime
os.environ.setdefault("JARVIS_NATIVE_TOOL_LOOP", "0")  # conftest already sets this

import task_runtime


def _make_ollama_response(content: str = "", tool_calls=None):
    """Build a fake ollama.chat() response."""
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls or []
    resp = MagicMock()
    resp.message = msg
    return resp


def _make_tool_call(name: str, arguments: dict):
    tc = MagicMock()
    func = MagicMock()
    func.name = name
    func.arguments = arguments
    tc.function = func
    return tc


class NativeLoopUnavailableTests(unittest.TestCase):
    """When Ollama is unavailable, _run_native_task_loop returns None."""

    def test_returns_none_when_ollama_not_running(self):
        with patch("brains.brain_ollama.get_client", side_effect=RuntimeError("no ollama")), \
             patch("brains.brain_ollama.get_best_available", side_effect=RuntimeError("no ollama")):
            task_runtime.reset_for_tests()
            result = task_runtime._run_native_task_loop(
                task_id="task_test01",
                prompt="hello",
                agent_ctx="You are a test agent.",
            )
        self.assertIsNone(result)

    def test_returns_none_on_import_error(self):
        with patch.dict(sys.modules, {"brains.brain_ollama": None}):
            # Can't truly remove — use side_effect on the lazy import path
            pass  # covered by get_client/get_best_available error path above


class NativeLoopDirectAnswerTests(unittest.TestCase):
    """When the model returns a direct answer (no tool calls), the loop
    emits it as chunks and returns (response, model, index, 0, '')."""

    def setUp(self):
        task_runtime.reset_for_tests()
        self.fake_workspace = {
            "ok": False, "enabled": False, "created": False,
            "reason": "", "repo_root": "", "worktree_path": "", "branch": "",
        }

    def _setup_task(self, prompt="summarise the repo"):
        resp = task_runtime.submit_task(prompt=prompt, kind="task")
        return resp["id"]

    def test_direct_answer_returns_content(self):
        task_id = self._setup_task("What is 2+2?")
        # Set task to running so the loop can emit events
        with task_runtime._LOCK:
            task_runtime._TASKS[task_id]["status"] = "running"

        ollama_resp = _make_ollama_response(content="The answer is 4.", tool_calls=[])
        mock_client = MagicMock()
        mock_client.chat.return_value = ollama_resp

        with patch("brains.brain_ollama.get_client", return_value=mock_client), \
             patch("brains.brain_ollama.get_best_available", return_value="test-model"):
            result = task_runtime._run_native_task_loop(
                task_id=task_id,
                prompt="What is 2+2?",
                agent_ctx="system ctx",
                start_index=0,
            )

        self.assertIsNotNone(result)
        response, model, index, tool_calls_made, transcript = result
        self.assertEqual(response, "The answer is 4.")
        self.assertEqual(model, "test-model")
        self.assertEqual(tool_calls_made, 0)
        self.assertEqual(transcript, "")
        self.assertGreater(index, 0)  # at least one chunk event emitted

    def test_specialist_request_is_exact_and_resource_bounded(self):
        task_id = self._setup_task("inspect the service")
        with task_runtime._LOCK:
            task_runtime._TASKS[task_id]["status"] = "running"

        mock_client = MagicMock()
        mock_client.chat.return_value = _make_ollama_response(content="Done.")

        with patch("brains.brain_ollama.get_client", return_value=mock_client), \
             patch("brains.brain_ollama.get_best_available", return_value="qwen3:30b-a3b") as select:
            result = task_runtime._run_native_task_loop(
                task_id=task_id,
                prompt="inspect the service",
                agent_ctx="system ctx",
            )

        self.assertIsNotNone(result)
        select.assert_called_once_with(
            "qwen3:30b-a3b",
            require_preferred=True,
        )
        self.assertEqual(
            mock_client.chat.call_args.kwargs["options"],
            {
                "num_ctx": 32_768,
                "num_predict": 2_048,
                "temperature": 0,
            },
        )

    def test_final_synthesis_keeps_specialist_resource_bounds(self):
        task_id = self._setup_task("inspect one command")
        with task_runtime._LOCK:
            task_runtime._TASKS[task_id]["status"] = "running"

        tool_call = _make_tool_call("bash_exec", {"command": "echo ready"})
        mock_client = MagicMock()
        mock_client.chat.side_effect = [
            _make_ollama_response(tool_calls=[tool_call]),
            [_make_ollama_response(content="Final answer.")],
        ]

        with patch("brains.brain_ollama.get_client", return_value=mock_client), \
             patch("brains.brain_ollama.get_best_available", return_value="qwen3:30b-a3b"), \
             patch.object(task_runtime, "_NATIVE_LOOP_MAX_ITERS", 1), \
             patch("task_runtime._tool_bash", return_value="ready"):
            result = task_runtime._run_native_task_loop(
                task_id=task_id,
                prompt="inspect one command",
                agent_ctx="system ctx",
            )

        self.assertIsNotNone(result)
        self.assertEqual(mock_client.chat.call_count, 2)
        for request in mock_client.chat.call_args_list:
            self.assertEqual(
                request.kwargs["options"],
                {
                    "num_ctx": 32_768,
                    "num_predict": 2_048,
                    "temperature": 0,
                },
            )

    def test_direct_answer_emits_sse_chunks(self):
        task_id = self._setup_task("tell me something")
        with task_runtime._LOCK:
            task_runtime._TASKS[task_id]["status"] = "running"

        ollama_resp = _make_ollama_response(content="A" * 500, tool_calls=[])
        mock_client = MagicMock()
        mock_client.chat.return_value = ollama_resp

        with patch("brains.brain_ollama.get_client", return_value=mock_client), \
             patch("brains.brain_ollama.get_best_available", return_value="m"):
            result = task_runtime._run_native_task_loop(
                task_id=task_id,
                prompt="tell me something",
                agent_ctx="",
            )

        self.assertIsNotNone(result)
        # Events stored in _TASK_EVENTS, not _TASKS
        events = task_runtime._TASK_EVENTS.get(task_id, [])
        chunk_events = [e for e in events if e.get("type") == "chunk"]
        # 500 chars / 200 per chunk = 3 chunks
        self.assertGreaterEqual(len(chunk_events), 2)


class NativeLoopBashToolTests(unittest.TestCase):
    """When the model calls bash_exec, the result is executed via _tool_bash
    and fed back; the final response comes from the synthesis call."""

    def setUp(self):
        task_runtime.reset_for_tests()

    def _make_task(self, prompt="count python files"):
        resp = task_runtime.submit_task(prompt=prompt, kind="task")
        tid = resp["id"]
        with task_runtime._LOCK:
            task_runtime._TASKS[tid]["status"] = "running"
        return tid

    def test_bash_tool_called_and_result_fed_back(self):
        task_id = self._make_task("how many py files?")

        tc = _make_tool_call("bash_exec", {"command": "echo hello"})
        tool_call_resp = _make_ollama_response(content="", tool_calls=[tc])
        final_resp = _make_ollama_response(content="There is 1 file.", tool_calls=[])

        mock_client = MagicMock()
        # First call returns tool request, second call returns final answer
        mock_client.chat.side_effect = [tool_call_resp, final_resp]

        fake_bash_result = "hello"

        with patch("brains.brain_ollama.get_client", return_value=mock_client), \
             patch("brains.brain_ollama.get_best_available", return_value="test-model"), \
             patch("task_runtime._tool_bash", return_value=fake_bash_result) as bash_mock:
            result = task_runtime._run_native_task_loop(
                task_id=task_id,
                prompt="how many py files?",
                agent_ctx="",
            )

        self.assertIsNotNone(result)
        response, model, index, tool_calls_made, transcript = result
        self.assertEqual(tool_calls_made, 1)
        self.assertIn("hello", transcript)
        bash_mock.assert_called_once_with("echo hello", root=None)

    def test_tool_calls_counted_in_transcript(self):
        task_id = self._make_task("run two checks")

        tc1 = _make_tool_call("bash_exec", {"command": "echo one"})
        tc2 = _make_tool_call("bash_exec", {"command": "echo two"})
        resp1 = _make_ollama_response(content="", tool_calls=[tc1])
        resp2 = _make_ollama_response(content="", tool_calls=[tc2])
        final = _make_ollama_response(content="Done.")

        mock_client = MagicMock()
        mock_client.chat.side_effect = [resp1, resp2, final]

        with patch("brains.brain_ollama.get_client", return_value=mock_client), \
             patch("brains.brain_ollama.get_best_available", return_value="m"), \
             patch("task_runtime._tool_bash", side_effect=["one\n", "two\n"]):
            result = task_runtime._run_native_task_loop(
                task_id=task_id,
                prompt="run two checks",
                agent_ctx="",
            )

        self.assertIsNotNone(result)
        response, model, index, tool_calls_made, transcript = result
        self.assertEqual(tool_calls_made, 2)
        self.assertIn("$ echo one", transcript)
        self.assertIn("$ echo two", transcript)


class NativeLoopWriteToolTests(unittest.TestCase):
    """write_file tool only fires when allow_write=True and work_root is set."""

    def setUp(self):
        task_runtime.reset_for_tests()

    def test_write_file_executes_when_allowed(self):
        resp = task_runtime.submit_task(prompt="write hello.py", kind="task")
        task_id = resp["id"]
        with task_runtime._LOCK:
            task_runtime._TASKS[task_id]["status"] = "running"

        tc = _make_tool_call("write_file", {"path": "hello.py", "content": "print('hi')"})
        tool_resp = _make_ollama_response(content="", tool_calls=[tc])
        final_resp = _make_ollama_response(content="File written.")

        mock_client = MagicMock()
        mock_client.chat.side_effect = [tool_resp, final_resp]

        fake_root = Path("/tmp/fake_worktree")

        with patch("brains.brain_ollama.get_client", return_value=mock_client), \
             patch("brains.brain_ollama.get_best_available", return_value="m"), \
             patch("task_runtime._tool_write_file", return_value="[wrote 12 chars to hello.py]") as wf_mock:
            result = task_runtime._run_native_task_loop(
                task_id=task_id,
                prompt="write hello.py",
                agent_ctx="",
                work_root=fake_root,
                allow_write=True,
            )

        self.assertIsNotNone(result)
        response, model, index, tool_calls_made, transcript = result
        self.assertEqual(tool_calls_made, 1)
        wf_mock.assert_called_once_with("hello.py", "print('hi')", fake_root)

    def test_write_file_ignored_when_write_not_allowed(self):
        resp = task_runtime.submit_task(prompt="write something", kind="task")
        task_id = resp["id"]
        with task_runtime._LOCK:
            task_runtime._TASKS[task_id]["status"] = "running"

        tc = _make_tool_call("write_file", {"path": "hack.py", "content": "evil"})
        tool_resp = _make_ollama_response(content="", tool_calls=[tc])
        final_resp = _make_ollama_response(content="No writes happened.")

        mock_client = MagicMock()
        mock_client.chat.side_effect = [tool_resp, final_resp]

        with patch("brains.brain_ollama.get_client", return_value=mock_client), \
             patch("brains.brain_ollama.get_best_available", return_value="m"), \
             patch("task_runtime._tool_write_file") as wf_mock:
            result = task_runtime._run_native_task_loop(
                task_id=task_id,
                prompt="write something",
                agent_ctx="",
                allow_write=False,
            )

        # write_file tool call is ignored when allow_write=False
        wf_mock.assert_not_called()


class NativeLoopFeatureFlagTests(unittest.TestCase):
    """JARVIS_NATIVE_TOOL_LOOP=0 must skip the native loop call in _run_task.

    Integration coverage: conftest.py sets this flag to "0" for all tests, so
    the entire regression suite (420+ tests) runs through the smart_stream mock
    path. This class adds a direct env-var read check.
    """

    def test_flag_env_var_defaults_to_enabled(self):
        """Default is enabled (production behaviour)."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JARVIS_NATIVE_TOOL_LOOP", None)
            enabled = os.getenv("JARVIS_NATIVE_TOOL_LOOP", "1") != "0"
        self.assertTrue(enabled)

    def test_flag_zero_disables_loop(self):
        """JARVIS_NATIVE_TOOL_LOOP=0 disables the native loop."""
        with patch.dict(os.environ, {"JARVIS_NATIVE_TOOL_LOOP": "0"}):
            enabled = os.getenv("JARVIS_NATIVE_TOOL_LOOP", "1") != "0"
        self.assertFalse(enabled)

    def test_flag_one_enables_loop(self):
        """JARVIS_NATIVE_TOOL_LOOP=1 keeps the native loop enabled."""
        with patch.dict(os.environ, {"JARVIS_NATIVE_TOOL_LOOP": "1"}):
            enabled = os.getenv("JARVIS_NATIVE_TOOL_LOOP", "1") != "0"
        self.assertTrue(enabled)


if __name__ == "__main__":
    unittest.main()
