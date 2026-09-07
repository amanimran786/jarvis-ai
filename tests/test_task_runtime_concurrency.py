"""Tests for Build-2 concurrency hardening in task_runtime.

Covers:
  - Atomic check-and-claim guard in _run_task (non-queued status → no-op)
  - _expire_stale_leases() force-fails expired tasks
  - _expire_stale_leases() skips terminal, queued, and waiting_approval tasks
  - lease_expires_at is set on every submit_task call
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import task_runtime


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_runtime():
    task_runtime.reset_for_tests()
    yield
    task_runtime.reset_for_tests()


def _now_str() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expired_lease() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat()


def _future_lease() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=1200)).isoformat()


def _insert_task(task_id: str, status: str, lease_expires_at: str = "") -> None:
    """Directly insert a minimal task into _TASKS for test setup."""
    now = _now_str()
    task_runtime._TASKS[task_id] = {
        "id": task_id,
        "status": status,
        "assigned_agent_id": "backend-engineer",
        "prompt": "test prompt",
        "effective_prompt": "test prompt",
        "created_at": now,
        "updated_at": now,
        "finished_at": "",
        "result": "",
        "error": "",
        "model": "",
        "source": "test",
        "kind": "chat",
        "meta": {},
        "lease_expires_at": lease_expires_at or _future_lease(),
        "cancel_requested": False,
    }
    task_runtime._TASK_EVENTS[task_id] = []


# ── Atomic check-and-claim ───────────────────────────────────────────────────

class TestAtomicCheckAndClaim:
    def test_run_task_is_noop_for_already_running_task(self):
        task_id = "t_already_running"
        _insert_task(task_id, status="running")

        smart_stream_calls: list = []
        with patch("task_runtime.smart_stream", side_effect=lambda *a, **kw: smart_stream_calls.append(a)):
            task_runtime._run_task(task_id)

        assert not smart_stream_calls, "smart_stream must not be called on a non-queued task"
        assert task_runtime._TASKS[task_id]["status"] == "running"

    def test_run_task_is_noop_for_assigned_task(self):
        task_id = "t_already_assigned"
        _insert_task(task_id, status="assigned")

        smart_stream_calls: list = []
        with patch("task_runtime.smart_stream", side_effect=lambda *a, **kw: smart_stream_calls.append(a)):
            task_runtime._run_task(task_id)

        assert not smart_stream_calls
        assert task_runtime._TASKS[task_id]["status"] == "assigned"

    def test_run_task_is_noop_for_succeeded_task(self):
        task_id = "t_succeeded"
        _insert_task(task_id, status="succeeded")

        smart_stream_calls: list = []
        with patch("task_runtime.smart_stream", side_effect=lambda *a, **kw: smart_stream_calls.append(a)):
            task_runtime._run_task(task_id)

        assert not smart_stream_calls
        assert task_runtime._TASKS[task_id]["status"] == "succeeded"

    def test_run_task_is_noop_for_failed_task(self):
        task_id = "t_failed"
        _insert_task(task_id, status="failed")

        smart_stream_calls: list = []
        with patch("task_runtime.smart_stream", side_effect=lambda *a, **kw: smart_stream_calls.append(a)):
            task_runtime._run_task(task_id)

        assert not smart_stream_calls
        assert task_runtime._TASKS[task_id]["status"] == "failed"

    def test_run_task_proceeds_for_queued_task(self):
        # A queued task SHOULD be processed; verify it reaches smart_stream.
        task_id = "t_queued"
        _insert_task(task_id, status="queued")

        smart_stream_called = threading.Event()

        def _fake_stream(*args, **kwargs):
            smart_stream_called.set()
            return iter(["ok"]), "test-model"

        fake_wm = MagicMock()
        fake_wm.prepare_isolated_workspace.return_value = {
            "ok": False, "enabled": False, "created": False,
            "reason": "", "repo_root": "", "worktree_path": "", "branch": "",
        }

        with patch("task_runtime.smart_stream", side_effect=_fake_stream), \
             patch("task_runtime.worktree_manager", fake_wm), \
             patch("task_runtime.usage_tracker") as mock_ut, \
             patch("task_runtime._auto_verify"):
            mock_ut.current_seq.return_value = 0
            mock_ut.delta_usage.return_value = {}

            t = threading.Thread(target=task_runtime._run_task, args=(task_id,), daemon=True)
            t.start()
            smart_stream_called.wait(timeout=3.0)
            t.join(timeout=3.0)

        assert smart_stream_called.is_set(), "_run_task must invoke smart_stream for a queued task"


class TestResetIsolation:
    def test_reset_replaces_execution_primitives_held_by_untracked_worker(self):
        old_agent_lock = task_runtime._get_agent_lock("chat-router")
        old_semaphore = task_runtime._MODEL_SEMAPHORE
        assert old_agent_lock.acquire(blocking=False)
        assert old_semaphore.acquire(blocking=False)

        try:
            task_runtime.reset_for_tests()
            replacement_lock = task_runtime._get_agent_lock("chat-router")
            assert replacement_lock is not old_agent_lock
            assert task_runtime._MODEL_SEMAPHORE is not old_semaphore
            assert replacement_lock.acquire(blocking=False)
            replacement_lock.release()
            assert task_runtime._MODEL_SEMAPHORE.acquire(blocking=False)
            task_runtime._MODEL_SEMAPHORE.release()
        finally:
            old_semaphore.release()
            old_agent_lock.release()

    def test_reset_allows_new_task_while_stale_primitives_remain_held(self):
        old_agent_lock = task_runtime._get_agent_lock("jarvis-manager")
        old_semaphore = task_runtime._MODEL_SEMAPHORE
        assert old_agent_lock.acquire(blocking=False)
        assert old_semaphore.acquire(blocking=False)

        try:
            task_runtime.reset_for_tests()
            with patch(
                "task_runtime.smart_stream",
                return_value=(iter(["ok"]), "UnitTestModel"),
            ), patch(
                "task_runtime.evals.log_interaction",
                return_value={"id": "interaction_test"},
            ):
                task = task_runtime.submit_task(
                    "bounded webhook test",
                    kind="task",
                    source="webhook",
                )
                if task.get("status") == "waiting_approval":
                    task_runtime.approve_task(str(task["id"]))
                completed = task_runtime.wait_for_task(str(task["id"]), timeout=2.0)

            assert completed is not None
            assert completed["status"] == "succeeded"
        finally:
            old_semaphore.release()
            old_agent_lock.release()


# ── TTL watchdog / lease expiry ──────────────────────────────────────────────

class TestLeaseExpiry:
    def test_expire_stale_leases_fails_expired_running_task(self):
        task_id = "t_expired_running"
        _insert_task(task_id, status="running", lease_expires_at=_expired_lease())

        task_runtime._expire_stale_leases()

        assert task_runtime._TASKS[task_id]["status"] == "failed"
        assert task_runtime._TASKS[task_id]["error"] == "task_lease_expired"

    def test_expire_stale_leases_fails_expired_assigned_task(self):
        task_id = "t_expired_assigned"
        _insert_task(task_id, status="assigned", lease_expires_at=_expired_lease())

        task_runtime._expire_stale_leases()

        assert task_runtime._TASKS[task_id]["status"] == "failed"

    def test_expire_stale_leases_fails_expired_streaming_task(self):
        task_id = "t_expired_streaming"
        _insert_task(task_id, status="streaming", lease_expires_at=_expired_lease())

        task_runtime._expire_stale_leases()

        assert task_runtime._TASKS[task_id]["status"] == "failed"

    def test_expire_stale_leases_skips_queued_task(self):
        task_id = "t_expired_queued"
        _insert_task(task_id, status="queued", lease_expires_at=_expired_lease())

        task_runtime._expire_stale_leases()

        assert task_runtime._TASKS[task_id]["status"] == "queued"

    def test_expire_stale_leases_skips_terminal_tasks(self):
        for status in ("succeeded", "failed", "cancelled"):
            task_id = f"t_terminal_{status}"
            _insert_task(task_id, status=status, lease_expires_at=_expired_lease())

        task_runtime._expire_stale_leases()

        for status in ("succeeded", "failed", "cancelled"):
            task_id = f"t_terminal_{status}"
            assert task_runtime._TASKS[task_id]["status"] == status

    def test_expire_stale_leases_skips_waiting_approval(self):
        task_id = "t_waiting"
        _insert_task(task_id, status="waiting_approval", lease_expires_at=_expired_lease())

        task_runtime._expire_stale_leases()

        assert task_runtime._TASKS[task_id]["status"] == "waiting_approval"

    def test_expire_stale_leases_skips_unexpired_task(self):
        task_id = "t_fresh_running"
        _insert_task(task_id, status="running", lease_expires_at=_future_lease())

        task_runtime._expire_stale_leases()

        assert task_runtime._TASKS[task_id]["status"] == "running"

    def test_expire_stale_leases_skips_task_with_no_lease(self):
        task_id = "t_no_lease"
        _insert_task(task_id, status="running", lease_expires_at="")

        task_runtime._expire_stale_leases()

        assert task_runtime._TASKS[task_id]["status"] == "running"


# ── lease_expires_at set on submit ───────────────────────────────────────────

class TestLeaseSetOnSubmit:
    def test_submit_task_sets_lease_expires_at(self):
        fake_ws = {
            "ok": False, "enabled": False, "created": False,
            "reason": "", "repo_root": "", "worktree_path": "", "branch": "",
        }
        fake_wm = MagicMock()
        fake_wm.prepare_isolated_workspace.return_value = fake_ws

        with patch("task_runtime.worktree_manager", fake_wm), \
             patch("task_runtime._start_task_thread"):
            task = task_runtime.submit_task("do something", kind="chat", source="test")

        assert "lease_expires_at" in task, "submit_task must set lease_expires_at"
        # Lease should be in the future (at least a few minutes out).
        lease_ts = datetime.fromisoformat(task["lease_expires_at"]).timestamp()
        now_ts = datetime.now(timezone.utc).timestamp()
        assert lease_ts > now_ts + 60, "lease_expires_at must be well in the future"
