from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from harness.runtime_adapter import (
    RuntimeIsolationError,
    RuntimeMissingOutcome,
    RuntimePendingOutcome,
    RuntimePendingStatus,
    RuntimeTerminalOutcome,
    RuntimeTerminalStatus,
    TaskRuntimeAdapter,
)
from harness.task_contract import TaskBudget, TaskSpec


class FakeTaskRuntime:
    def __init__(self, *, workspace: dict[str, Any] | None = None) -> None:
        self.workspace = workspace or {
            "enabled": True,
            "ok": True,
            "worktree_path": "/tmp/jarvis-task-1",
            "branch": "jarvis/task-1",
        }
        self.tasks: dict[str, dict[str, Any]] = {}
        self.submissions: list[tuple[str, dict[str, Any]]] = []
        self.cancelled: list[str] = []

    def submit_task(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.submissions.append((prompt, kwargs))
        task = {
            "id": "runtime-task-1",
            "status": "queued",
            "workspace": dict(self.workspace),
            "result": "",
            "error": "",
            "cancel_requested": False,
        }
        self.tasks[task["id"]] = task
        return dict(task)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        task = self.tasks.get(task_id)
        return dict(task) if task is not None else None

    def cancel_task(self, task_id: str) -> dict[str, Any] | None:
        self.cancelled.append(task_id)
        task = self.tasks.get(task_id)
        if task is not None:
            task["cancel_requested"] = True
            return dict(task)
        return None


@pytest.fixture
def spec() -> TaskSpec:
    return TaskSpec(
        task_id="TASK-ADAPTER-1",
        title="Exercise runtime adapter",
        goal="Prove lifecycle mapping",
        description="Run one isolated code task",
        budget=TaskBudget(wall_time_seconds=30),
    )


def _submit(
    runtime: FakeTaskRuntime,
    spec: TaskSpec,
    *,
    now: float = 100.0,
) -> tuple[TaskRuntimeAdapter, Any]:
    adapter = TaskRuntimeAdapter(runtime, clock=lambda: now)
    correlation = adapter.submit(
        spec,
        "rendered TaskSpec prompt",
        attempt_id="attempt-1",
        base_ref="abc123",
    )
    return adapter, correlation


def test_submission_sets_runtime_metadata_and_returns_correlation(spec: TaskSpec) -> None:
    runtime = FakeTaskRuntime()

    _, correlation = _submit(runtime, spec)

    prompt, kwargs = runtime.submissions[0]
    assert prompt == "rendered TaskSpec prompt"
    assert kwargs == {
        "kind": "code",
        "source": "orchestrator_loop",
        "isolated_workspace": True,
        "meta": {
            "orchestrator_task_id": spec.task_id,
            "attempt_id": "attempt-1",
            "contract_sha256": spec.contract_hash,
            "base_ref": "abc123",
        },
    }
    assert "_trusted_runtime_meta" not in kwargs
    assert correlation.runtime_task_id == "runtime-task-1"
    assert correlation.attempt_id == "attempt-1"
    assert correlation.contract_hash == spec.contract_hash
    assert correlation.worktree_path == "/tmp/jarvis-task-1"
    assert correlation.base_ref == "abc123"
    assert correlation.deadline == 130.0


def test_runtime_correlation_round_trips_for_cross_loop_polling(spec: TaskSpec) -> None:
    runtime = FakeTaskRuntime()
    _, correlation = _submit(runtime, spec)

    restored = type(correlation).from_dict(correlation.to_dict())

    assert restored == correlation


@pytest.mark.parametrize(
    "workspace",
    [
        {"enabled": False, "ok": True, "worktree_path": "/tmp/task"},
        {"enabled": True, "ok": False, "worktree_path": "/tmp/task"},
        {"enabled": True, "ok": True, "worktree_path": ""},
    ],
)
def test_submission_fails_closed_when_isolation_is_unavailable(
    spec: TaskSpec,
    workspace: dict[str, Any],
) -> None:
    runtime = FakeTaskRuntime(workspace=workspace)
    adapter = TaskRuntimeAdapter(runtime, clock=lambda: 100.0)

    with pytest.raises(RuntimeIsolationError) as exc_info:
        adapter.submit(
            spec,
            "rendered prompt",
            attempt_id="attempt-1",
            base_ref="abc123",
        )

    assert exc_info.value.runtime_task_id == "runtime-task-1"
    assert runtime.cancelled == ["runtime-task-1"]


def test_waiting_approval_is_reported_without_approval(spec: TaskSpec) -> None:
    runtime = FakeTaskRuntime()
    adapter, correlation = _submit(runtime, spec)
    runtime.tasks[correlation.runtime_task_id].update(
        status="waiting_approval",
        approval_reason="operator review required",
    )

    outcome = adapter.poll(correlation)

    assert outcome == RuntimePendingOutcome(
        correlation=correlation,
        status=RuntimePendingStatus.WAITING_APPROVAL,
        approval_reason="operator review required",
    )
    assert not hasattr(runtime, "approve_task")
    assert runtime.cancelled == []


@pytest.mark.parametrize(
    ("status", "expected_status", "result", "error"),
    [
        ("succeeded", RuntimeTerminalStatus.SUCCEEDED, "completed", ""),
        ("failed", RuntimeTerminalStatus.FAILED, "partial", "execution failed"),
        ("cancelled", RuntimeTerminalStatus.CANCELLED, "", "operator cancelled"),
    ],
)
def test_terminal_runtime_states_map_to_typed_outcomes(
    spec: TaskSpec,
    status: str,
    expected_status: RuntimeTerminalStatus,
    result: str,
    error: str,
) -> None:
    runtime = FakeTaskRuntime()
    adapter, correlation = _submit(runtime, spec)
    runtime.tasks[correlation.runtime_task_id].update(
        status=status,
        result=result,
        error=error,
    )

    outcome = adapter.poll(correlation)

    assert outcome == RuntimeTerminalOutcome(
        correlation=correlation,
        status=expected_status,
        result=result,
        error=error,
    )


def test_timeout_requests_cancellation_once_until_runtime_is_terminal(spec: TaskSpec) -> None:
    runtime = FakeTaskRuntime()
    adapter, correlation = _submit(runtime, spec, now=100.0)
    adapter = TaskRuntimeAdapter(runtime, clock=lambda: 131.0)
    runtime.tasks[correlation.runtime_task_id]["status"] = "running"

    first = adapter.poll(correlation)
    second = adapter.poll(correlation)

    assert first == RuntimePendingOutcome(
        correlation=correlation,
        status=RuntimePendingStatus.TIMEOUT_PENDING,
    )
    assert second == first
    assert runtime.cancelled == [correlation.runtime_task_id]

    runtime.tasks[correlation.runtime_task_id].update(
        status="cancelled",
        error="deadline exceeded",
    )
    terminal = adapter.poll(correlation)
    assert terminal == RuntimeTerminalOutcome(
        correlation=correlation,
        status=RuntimeTerminalStatus.CANCELLED,
        result="",
        error="deadline exceeded",
    )


def test_poll_reports_missing_runtime_task(spec: TaskSpec) -> None:
    runtime = FakeTaskRuntime()
    adapter, correlation = _submit(runtime, spec)
    runtime.tasks.clear()

    outcome = adapter.poll(replace(correlation))

    assert outcome == RuntimeMissingOutcome(
        correlation=correlation,
        error="runtime task not found: runtime-task-1",
    )
