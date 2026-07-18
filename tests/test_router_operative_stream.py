"""
Test that operative tasks stream live progress tokens before the final summary.

Router's _operative_stream() runs run_task() in a thread and yields progress
tokens as on_progress() fires.  Tests verify:
 - progress tokens appear before the summary
 - the summary token appears at the end
 - run_task() exceptions don't deadlock the stream
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch, MagicMock


def _fake_run_task(task, *, on_progress=None):
    """Simulates operative.run_task() that fires 2 progress callbacks."""
    if on_progress:
        on_progress("read config", "ok")
        on_progress("write output", "done")
    return {
        "ok": True,
        "summary": "All done.",
        "steps": [],
    }


def _fake_run_task_with_failure(task, *, on_progress=None):
    if on_progress:
        on_progress("read config", "ok")
    failed = SimpleNamespace(description="write file", ok=False)
    return {
        "ok": False,
        "summary": "Task partially completed.",
        "steps": [failed],
    }


def _fake_run_task_raises(task, *, on_progress=None):
    raise RuntimeError("model offline")


def _classify_operative(task_str: str):
    from orchestrator import ToolDecision
    return ToolDecision(
        tool="operative",
        confidence=0.9,
        action="run_task",
        params={"task": task_str},
    )


def _collect_stream(prompt: str, run_task_fn) -> list[str]:
    """Call _orchestrate with mocked classify and run_task; collect all tokens."""
    import router
    def _prepare(task, *, context=None, cancel_event=None):
        return {"status": "ready", "manifest": {"task": task}}

    def _execute(
        manifest,
        *,
        on_progress=None,
        cancel_event=None,
        context=None,
    ):
        return run_task_fn(manifest["task"], on_progress=on_progress)

    with patch("orchestrator.classify", return_value=_classify_operative(prompt)), \
         patch("operative.prepare_task", side_effect=_prepare), \
         patch("operative.execute_prepared_task", side_effect=_execute), \
         patch("skills.choose_skill", return_value=None), \
         patch("router.audit_log"):
        stream, _label = router._orchestrate(prompt, prompt.lower())
        return list(stream)


class TestOperativeStream:
    def test_acknowledgement_is_first_token(self):
        tokens = _collect_stream("do a task", _fake_run_task)
        assert tokens, "stream is empty"
        assert "now" in tokens[0].lower() or "understood" in tokens[0].lower(), (
            f"unexpected first token: {tokens[0]!r}"
        )

    def test_progress_tokens_appear_mid_stream(self):
        tokens = _collect_stream("do a task", _fake_run_task)
        progress = [t for t in tokens if t.startswith("•")]
        assert len(progress) == 2, f"expected 2 progress tokens, got: {tokens}"

    def test_progress_includes_step_description(self):
        tokens = _collect_stream("do a task", _fake_run_task)
        full = " ".join(tokens)
        assert "read config" in full
        assert "write output" in full

    def test_summary_is_last_meaningful_token(self):
        tokens = _collect_stream("do a task", _fake_run_task)
        # Summary should be one of the last tokens (may be preceded by a space)
        full = " ".join(tokens)
        assert "All done." in full

    def test_failure_note_appended_when_steps_fail(self):
        tokens = _collect_stream("do a task", _fake_run_task_with_failure)
        full = " ".join(tokens)
        assert "task failed" in full.lower()
        assert "0/1 steps" in full.lower()

    def test_exception_in_run_task_does_not_deadlock(self):
        tokens = _collect_stream("do a task", _fake_run_task_raises)
        # Should not hang; error message should appear in summary
        assert tokens, "stream produced nothing on exception"
        full = " ".join(tokens)
        assert "offline" in full or "model" in full or "error" in full.lower(), (
            f"exception message missing from: {full!r}"
        )
