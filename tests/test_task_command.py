"""
Tests for /task router command.

Verifies: detection, prefix stripping, streaming generator structure,
empty-arg guard, and end-to-end output through a mocked operative.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
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

def _fake_run_task(task, on_progress=None, cancel_event=None):
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


@contextmanager
def _mock_task_execution(run_fn):
    def _prepare(task, *, context=None, cancel_event=None):
        return {"status": "ready", "manifest": {"task": task}}

    def _execute(manifest, on_progress=None, cancel_event=None, *, context=None):
        return run_fn(
            manifest["task"],
            on_progress=on_progress,
            cancel_event=cancel_event,
        )

    with patch("operative.prepare_task", side_effect=_prepare), patch(
        "operative.execute_prepared_task", side_effect=_execute
    ):
        yield


class TestTaskCommandStream:
    def test_empty_task_yields_usage_hint(self):
        chunks = list(_task_command_stream(""))
        combined = "".join(chunks).lower()
        assert "usage" in combined

    def test_yields_task_header(self):
        with _mock_task_execution(_fake_run_task):
            chunks = list(_task_command_stream("write a haiku"))
        combined = "".join(chunks)
        assert "write a haiku" in combined

    def test_yields_progress_bullets(self):
        with _mock_task_execution(_fake_run_task):
            chunks = list(_task_command_stream("write a haiku"))
        combined = "".join(chunks)
        assert "•" in combined
        assert "Planning task" in combined

    def test_yields_completion_line(self):
        with _mock_task_execution(_fake_run_task):
            chunks = list(_task_command_stream("write a haiku"))
        combined = "".join(chunks)
        assert "Done" in combined or "Partial" in combined

    def test_yields_summary(self):
        with _mock_task_execution(_fake_run_task):
            chunks = list(_task_command_stream("write a haiku"))
        combined = "".join(chunks)
        assert "Wrote a haiku and saved it." in combined

    def test_error_path_yields_failure_message(self):
        def _bad_run(task, on_progress=None, cancel_event=None):
            raise RuntimeError("local model unavailable")

        with _mock_task_execution(_bad_run):
            chunks = list(_task_command_stream("do something"))
        combined = "".join(chunks)
        assert "failed" in combined.lower() or "error" in combined.lower()

    def test_partial_result_when_steps_fail(self):
        def _partial_run(task, on_progress=None, cancel_event=None):
            ok_step = MagicMock(); ok_step.ok = True; ok_step.description = "step1"
            fail_step = MagicMock(); fail_step.ok = False; fail_step.description = "step2"
            return {
                "task": task,
                "steps": [ok_step, fail_step],
                "summary": "Only half done.",
                "results": {},
                "ok": False,
            }

        with _mock_task_execution(_partial_run):
            chunks = list(_task_command_stream("two-step task"))
        combined = "".join(chunks)
        assert "Partial" in combined or "1/2" in combined

    def test_generator_close_cancels_worker_and_releases_session(self):
        import router
        from operative_approval import RouteContext

        context = RouteContext("tester", "close-session", "pytest", True)
        started = threading.Event()
        release = threading.Event()
        observed: dict[str, threading.Event] = {}

        def _worker(_on_progress, cancel):
            observed["cancel"] = cancel
            started.set()
            cancel.wait(1)
            release.wait(1)
            return {"ok": False, "summary": "cancelled", "steps": []}

        stream = router._run_task_worker_stream(
            "header",
            _worker,
            context=context,
            thread_name="TaskCloseTest",
        )
        assert next(stream) == "header"
        assert started.wait(1)

        stream.close()

        assert observed["cancel"].is_set()
        replacement = threading.Event()
        assert router._register_task_cancel(context, replacement) is False
        release.set()
        for _ in range(100):
            if router._register_task_cancel(context, replacement):
                break
            threading.Event().wait(0.01)
        else:
            pytest.fail("session was not released after worker termination")
        router._clear_task_cancel(context, replacement)

    def test_timeout_cancels_worker_and_reports_failure(self):
        import router
        from operative_approval import RouteContext

        context = RouteContext("tester", "timeout-session", "pytest", True)
        observed: dict[str, threading.Event] = {}

        def _worker(_on_progress, cancel):
            observed["cancel"] = cancel
            cancel.wait(1)
            return {"ok": False, "summary": "cancelled", "steps": []}

        with patch("config.OPERATIVE_TIMEOUT_SECONDS", 0.01):
            output = "".join(
                router._run_task_worker_stream(
                    "header\n",
                    _worker,
                    context=context,
                    thread_name="TaskTimeoutTest",
                )
            )

        assert "timeout" in output.lower()
        assert observed["cancel"].is_set()

    def test_cancel_is_scoped_to_route_context(self):
        import router
        from operative_approval import RouteContext

        first = RouteContext("tester", "session-one", "pytest", True)
        second = RouteContext("tester", "session-two", "pytest", True)
        first_event = threading.Event()
        second_event = threading.Event()
        assert router._register_task_cancel(first, first_event) is True
        assert router._register_task_cancel(second, second_event) is True
        try:
            reply = router._cancel_task_reply(first)
        finally:
            router._clear_task_cancel(first, first_event)
            router._clear_task_cancel(second, second_event)

        assert "Cancelling" in reply
        assert first_event.is_set()
        assert not second_event.is_set()


# ── Route integration ──────────────────────────────────────────────────────────

class TestRouteStreamIntegration:
    def test_route_stream_returns_operative_label(self):
        """route_stream('/task X') must return (iterator, 'Operative')."""
        with _mock_task_execution(_fake_run_task):
            from router import route_stream
            stream, label = route_stream("/task write a haiku")
            chunks = list(stream)
        assert label == "Operative"
        assert chunks

    def test_route_stream_streams_chunks(self):
        with _mock_task_execution(_fake_run_task):
            from router import route_stream
            stream, _ = route_stream("/task write a haiku")
            chunks = list(stream)
        assert len(chunks) > 1, "Expected multiple streaming chunks"

    def test_approval_command_bypasses_pending_email_draft(self):
        import router

        router._set_pending_email_draft(
            "Alice",
            "alice@example.com",
            "Pending subject",
            "Pending body",
        )
        try:
            result = {"ok": True, "steps": [], "summary": "approved"}
            with patch("operative.approve_and_run_task", return_value=result) as approve:
                stream, label = router.route_stream(
                    "/task approve op_123456789012"
                )
                response = "".join(stream)
        finally:
            router._clear_pending_email_draft()

        assert label == "Operative"
        assert "approved" in response
        approve.assert_called_once()

    def test_active_task_cancel_bypasses_pending_message_draft(self):
        import router
        from operative_approval import RouteContext

        context = RouteContext("tester", "cancel-draft", "pytest", True)
        cancel = threading.Event()
        router._set_pending_message_draft("Alice", "Pending body")
        assert router._register_task_cancel(context, cancel) is True
        try:
            stream, label = router.route_stream("/cancel task", context=context)
            response = "".join(stream)
        finally:
            router._clear_task_cancel(context, cancel)
            router._clear_pending_message_draft()

        assert label == "Operative"
        assert "Cancelling" in response
        assert cancel.is_set()
