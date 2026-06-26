"""Unit tests for coder_workbench.fix_loop and _parse_coder_json."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import coder_workbench


# ── helpers ───────────────────────────────────────────────────────────────────

def _coder_response(files: list[dict], test_command: str) -> str:
    return json.dumps({"files": files, "test_command": test_command})


def _ollama_response(content: str) -> MagicMock:
    """Build a mock that looks like ollama.Client.chat() return value."""
    msg = SimpleNamespace(content=content)
    return SimpleNamespace(message=msg)


# ── _parse_coder_json ─────────────────────────────────────────────────────────

class TestParseCoderJson:
    def test_bare_json_object(self):
        raw = '{"files": [], "test_command": "pytest"}'
        result = coder_workbench._parse_coder_json(raw)
        assert result["test_command"] == "pytest"

    def test_markdown_fenced(self):
        raw = '```json\n{"files": [], "test_command": "pytest -q"}\n```'
        result = coder_workbench._parse_coder_json(raw)
        assert result["test_command"] == "pytest -q"

    def test_json_embedded_in_prose(self):
        raw = 'Here is the plan:\n{"files": [{"path": "foo.py", "content": "x=1"}], "test_command": ""}\nDone.'
        result = coder_workbench._parse_coder_json(raw)
        assert result["files"][0]["path"] == "foo.py"

    def test_raises_on_no_json(self):
        with pytest.raises(ValueError, match="No JSON"):
            coder_workbench._parse_coder_json("just some text with no braces at all")


# ── fix_loop ──────────────────────────────────────────────────────────────────

class TestFixLoop:
    def _chat_side_effect(self, responses):
        """Return side_effect iterable for ollama.Client.chat mock."""
        it = iter(responses)
        def _side(self_arg=None, **kwargs):
            r = next(it)
            if isinstance(r, Exception):
                raise r
            return r
        return _side

    def test_success_first_iteration(self, tmp_path):
        payload = _coder_response(
            files=[{"path": "hello.py", "content": "print('hello')"}],
            test_command="python hello.py",
        )
        chat_resp = _ollama_response(payload)
        with patch("ollama.Client.chat", return_value=chat_resp), \
             patch("brains.brain_ollama.get_best_available", return_value="devstral"), \
             patch("coder_workbench._run_shell", return_value=(0, "hello", 0.1)):
            result = coder_workbench.fix_loop("say hello", workspace=tmp_path)

        assert result["ok"] is True
        assert result["iterations"] == 1
        assert "hello.py" in result["files"]
        assert result["test_command"] == "python hello.py"
        assert len(result["history"]) == 1
        assert result["history"][0]["ok"] is True

    def test_fix_on_second_iteration(self, tmp_path):
        payload1 = _coder_response(
            files=[{"path": "add.py", "content": "def add(a,b): return a-b"}],
            test_command="pytest test_add.py -q",
        )
        payload2 = _coder_response(
            files=[{"path": "add.py", "content": "def add(a,b): return a+b"}],
            test_command="pytest test_add.py -q",
        )
        chat_responses = [_ollama_response(payload1), _ollama_response(payload2)]
        run_outputs = [(1, "FAILED assert 3 == 5", 0.2), (0, "1 passed", 0.1)]
        with patch("ollama.Client.chat", side_effect=chat_responses), \
             patch("brains.brain_ollama.get_best_available", return_value="devstral"), \
             patch("coder_workbench._run_shell", side_effect=run_outputs):
            result = coder_workbench.fix_loop("implement add()", workspace=tmp_path, max_iterations=3)

        assert result["ok"] is True
        assert result["iterations"] == 2
        assert len(result["history"]) == 2
        assert result["history"][0]["ok"] is False
        assert result["history"][1]["ok"] is True

    def test_max_iterations_exceeded(self, tmp_path):
        payload = _coder_response(
            files=[{"path": "broken.py", "content": "1/0"}],
            test_command="python broken.py",
        )
        with patch("ollama.Client.chat", return_value=_ollama_response(payload)), \
             patch("brains.brain_ollama.get_best_available", return_value="devstral"), \
             patch("coder_workbench._run_shell", return_value=(1, "ZeroDivisionError", 0.1)):
            result = coder_workbench.fix_loop("broken task", workspace=tmp_path, max_iterations=2)

        assert result["ok"] is False
        assert result["iterations"] == 2
        assert "error" in result
        assert len(result["history"]) == 2

    def test_path_traversal_rejected(self, tmp_path):
        payload = _coder_response(
            files=[{"path": "../../etc/passwd", "content": "hacked"}],
            test_command="echo done",
        )
        with patch("ollama.Client.chat", return_value=_ollama_response(payload)), \
             patch("brains.brain_ollama.get_best_available", return_value="devstral"), \
             patch("coder_workbench._run_shell", return_value=(0, "", 0.0)):
            result = coder_workbench.fix_loop("path traversal attempt", workspace=tmp_path, max_iterations=1)

        assert result["ok"] is False
        assert "Path traversal" in result.get("error", "")
        assert not Path("/etc/passwd.hacked").exists()

    def test_model_error_skips_iteration(self, tmp_path):
        good_payload = _coder_response(
            files=[{"path": "ok.py", "content": "pass"}],
            test_command="python ok.py",
        )
        responses = [ConnectionError("Ollama not running"), _ollama_response(good_payload)]
        with patch("ollama.Client.chat", side_effect=responses), \
             patch("brains.brain_ollama.get_best_available", return_value="devstral"), \
             patch("coder_workbench._run_shell", return_value=(0, "ok", 0.1)):
            result = coder_workbench.fix_loop("simple task", workspace=tmp_path, max_iterations=3)

        assert result["ok"] is True
        assert result["iterations"] == 2

    def test_auto_infers_test_command_from_test_files(self, tmp_path):
        payload = _coder_response(
            files=[
                {"path": "math_utils.py", "content": "def add(a,b): return a+b"},
                {"path": "test_math.py", "content": "from math_utils import add\ndef test_add(): assert add(1,2)==3"},
            ],
            test_command="",
        )
        with patch("ollama.Client.chat", return_value=_ollama_response(payload)), \
             patch("brains.brain_ollama.get_best_available", return_value="devstral"), \
             patch("coder_workbench._run_shell", return_value=(0, "1 passed", 0.1)) as mock_run:
            result = coder_workbench.fix_loop("math utils", workspace=tmp_path, max_iterations=1)

        assert result["ok"] is True
        called_cmd = mock_run.call_args[0][0]
        assert "test_math.py" in called_cmd
        assert "pytest" in called_cmd
