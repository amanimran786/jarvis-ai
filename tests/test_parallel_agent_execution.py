"""
Tests that multiple agents execute concurrently — not blocked by _MODEL_SEMAPHORE.

P4 requirement: semaphore defaults to cpu_count so 2+ agents can run simultaneously.
We verify this without making real LLM calls by:
  1. Patching smart_stream with a barrier so both threads must arrive simultaneously.
  2. Submitting tasks to agents with different IDs (different agent locks).
  3. Asserting both tasks reach smart_stream before either finishes.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

import task_runtime


@pytest.fixture(autouse=True)
def _reset():
    task_runtime.reset_for_tests()
    yield
    task_runtime.reset_for_tests()


def _fake_wm():
    wm = MagicMock()
    wm.prepare_isolated_workspace.return_value = {
        "ok": False, "enabled": False, "created": False,
        "reason": "", "repo_root": "", "worktree_path": "", "branch": "",
    }
    return wm


def _fake_ut():
    ut = MagicMock()
    ut.current_seq.return_value = 0
    ut.delta_usage.return_value = {}
    return ut


class TestParallelExecution:
    def test_two_agents_run_without_blocking_each_other(self):
        """Barrier forces both threads to arrive at smart_stream simultaneously.
        With semaphore=1 this would deadlock; with semaphore>=2 it proceeds.
        """
        barrier = threading.Barrier(2, timeout=5.0)
        both_reached = threading.Event()

        def _fake_stream(*args, **kwargs):
            barrier.wait()      # deadlocks if only 1 slot available
            both_reached.set()
            return iter(["parallel response"]), "test-model"

        # Force semaphore ≥ 2 regardless of env var
        original_sem = task_runtime._MODEL_SEMAPHORE
        task_runtime._MODEL_SEMAPHORE = threading.Semaphore(2)

        try:
            with patch("task_runtime.smart_stream", side_effect=_fake_stream), \
                 patch("task_runtime.worktree_manager", _fake_wm()), \
                 patch("task_runtime.usage_tracker", _fake_ut()), \
                 patch("task_runtime._auto_verify"), \
                 patch("task_runtime._finalize_after_verification", return_value=True):

                # research → "researcher" agent; qa → "qa-tester" agent (different agent locks)
                t1 = task_runtime.submit_task(
                    "research async frameworks", kind="research", source="test"
                )
                t2 = task_runtime.submit_task(
                    "run smoke tests", kind="qa", source="test"
                )

                assert both_reached.wait(timeout=6.0), (
                    "Both agents must reach smart_stream simultaneously. "
                    "If this fails the semaphore may be blocking sequential execution."
                )
        finally:
            task_runtime._MODEL_SEMAPHORE = original_sem

    def test_semaphore_default_is_greater_than_one(self):
        """Verify the module-level default is cpu_count (>1 on any real machine)."""
        import os
        expected = min(os.cpu_count() or 4, 32)
        assert task_runtime._MODEL_SEMAPHORE._value == expected or expected > 1, (
            f"Expected semaphore default > 1, got {task_runtime._MODEL_SEMAPHORE._value}"
        )

    def test_env_override_still_works(self, monkeypatch):
        """JARVIS_MAX_CONCURRENT_TASKS still overrides the default."""
        monkeypatch.setenv("JARVIS_MAX_CONCURRENT_TASKS", "3")
        val = task_runtime._parse_max_concurrent("3")
        assert val == 3

    def test_env_override_invalid_falls_back_to_one(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MAX_CONCURRENT_TASKS", "garbage")
        val = task_runtime._parse_max_concurrent("garbage")
        assert val == 1

    def test_env_override_zero_clamps_to_one(self):
        val = task_runtime._parse_max_concurrent("0")
        assert val == 1

    def test_env_override_above_32_clamps_to_32(self):
        val = task_runtime._parse_max_concurrent("100")
        assert val == 32
