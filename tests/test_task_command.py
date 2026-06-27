"""
Tests for /task router command.

Verifies: detection, prefix stripping, streaming generator structure,
empty-arg guard, and end-to-end output through a mocked operative.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from router import _is_task_command, _task_command_stream


# ── Detection ──────────────────────────────────────────────────────────────────

class TestIsTaskCommand:
    def test_slash_task_with_description(self):
        assert _is_task_command("/task write a haiku") is True

    def test_slash_task_alone(self):
        assert _is_task_command("/task") is True

    def test_slash_task_leading_whitespace(self):
        assert _is_task_command("  /task write something  ") is True

    def test_score_not_matched(self):
        assert _is_task_command("/score") is False

    def test_task_without_slash(self):
        assert _is_task_command("task do something") is False

    def test_empty_string(self):
        assert _is_task_command("") is False


# ── Streaming generator ────────────────────────────────────────────────────────

def _fake_run_task(task, on_progress=None):
    """Minimal run_task stub that fires two progress calls then returns."""
    if on_progress:
        on_progress("Planning task", task)
        on_progress("Step 1: write haiku", "Silicon dreams / Data flows like rivers / Logic never lies")
    step = MagicMock()
    step.ok = True
    step.description = "write haiku"
    return {
        "task": task,
        "steps": [step],
        "summary": "Wrote a haiku and saved it.",
        "results": {1: "Silicon dreams / Data flows..."},
        "ok": True,
    }


class TestTaskCommandStream:
    def test_empty_task_yields_usage_hint(self):
        chunks = list(_task_command_stream(""))
        combined = "".join(chunks).lower()
        assert "usage" in combined

    def test_yields_task_header(self):
        with patch("operative.run_task", side_effect=_fake_run_task):
            chunks = list(_task_command_stream("write a haiku"))
        combined = "".join(chunks)
        assert "write a haiku" in combined

    def test_yields_progress_bullets(self):
        with patch("operative.run_task", side_effect=_fake_run_task):
            chunks = list(_task_command_stream("write a haiku"))
        combined = "".join(chunks)
        assert "•" in combined
        assert "Planning task" in combined

    def test_yields_completion_line(self):
        with patch("operative.run_task", side_effect=_fake_run_task):
            chunks = list(_task_command_stream("write a haiku"))
        combined = "".join(chunks)
        assert "Done" in combined or "Partial" in combined

    def test_yields_summary(self):
        with patch("operative.run_task", side_effect=_fake_run_task):
            chunks = list(_task_command_stream("write a haiku"))
        combined = "".join(chunks)
        assert "Wrote a haiku and saved it." in combined

    def test_error_path_yields_failure_message(self):
        def _bad_run(task, on_progress=None):
            raise RuntimeError("local model unavailable")

        with patch("operative.run_task", side_effect=_bad_run):
            chunks = list(_task_command_stream("do something"))
        combined = "".join(chunks)
        assert "failed" in combined.lower() or "error" in combined.lower()

    def test_partial_result_when_steps_fail(self):
        def _partial_run(task, on_progress=None):
            ok_step = MagicMock(); ok_step.ok = True; ok_step.description = "step1"
            fail_step = MagicMock(); fail_step.ok = False; fail_step.description = "step2"
            return {
                "task": task,
                "steps": [ok_step, fail_step],
                "summary": "Only half done.",
                "results": {},
                "ok": False,
            }

        with patch("operative.run_task", side_effect=_partial_run):
            chunks = list(_task_command_stream("two-step task"))
        combined = "".join(chunks)
        assert "Partial" in combined or "1/2" in combined


# ── Route integration ──────────────────────────────────────────────────────────

class TestRouteStreamIntegration:
    def test_route_stream_returns_operative_label(self):
        """route_stream('/task X') must return (iterator, 'Operative')."""
        with patch("operative.run_task", side_effect=_fake_run_task):
            from router import route_stream
            stream, label = route_stream("/task write a haiku")
        assert label == "Operative"
        # Consume so the thread finishes cleanly
        list(stream)

    def test_route_stream_streams_chunks(self):
        with patch("operative.run_task", side_effect=_fake_run_task):
            from router import route_stream
            stream, _ = route_stream("/task write a haiku")
            chunks = list(stream)
        assert len(chunks) > 1, "Expected multiple streaming chunks"
