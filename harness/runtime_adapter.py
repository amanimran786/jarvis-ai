"""Deterministic lifecycle bridge from orchestration attempts to task_runtime."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Protocol

from harness.task_contract import TaskSpec


class TaskRuntimeModule(Protocol):
    """The task_runtime surface used by the adapter."""

    def submit_task(self, prompt: str, **kwargs: Any) -> dict[str, Any]: ...

    def get_task(self, task_id: str) -> dict[str, Any] | None: ...

    def cancel_task(self, task_id: str) -> dict[str, Any] | None: ...


class RuntimeAdapterError(RuntimeError):
    """Base error for adapter contract failures."""


class RuntimeIsolationError(RuntimeAdapterError):
    """Raised when task_runtime does not provide the required worktree."""

    def __init__(self, message: str, *, runtime_task_id: str = "") -> None:
        super().__init__(message)
        self.runtime_task_id = runtime_task_id


class RuntimeProtocolError(RuntimeAdapterError):
    """Raised when task_runtime returns a malformed or unknown state."""


class RuntimePendingStatus(str, Enum):
    QUEUED = "queued"
    ASSIGNED = "assigned"
    RUNNING = "running"
    STREAMING = "streaming"
    WAITING_APPROVAL = "waiting_approval"
    TIMEOUT_PENDING = "timeout_pending"


class RuntimeTerminalStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RuntimeCorrelation:
    """Durable identifiers needed to poll one runtime-backed attempt."""

    runtime_task_id: str
    attempt_id: str
    contract_hash: str
    worktree_path: str
    base_ref: str
    deadline: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_task_id": self.runtime_task_id,
            "attempt_id": self.attempt_id,
            "contract_hash": self.contract_hash,
            "worktree_path": self.worktree_path,
            "base_ref": self.base_ref,
            "deadline": self.deadline,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RuntimeCorrelation":
        if not isinstance(value, Mapping):
            raise TypeError("runtime correlation must be an object")
        deadline = value.get("deadline")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            raise RuntimeProtocolError("runtime correlation deadline must be numeric")
        deadline_value = float(deadline)
        if not math.isfinite(deadline_value):
            raise RuntimeProtocolError("runtime correlation deadline must be finite")
        return cls(
            runtime_task_id=_required_text(value.get("runtime_task_id"), "runtime_task_id"),
            attempt_id=_required_text(value.get("attempt_id"), "attempt_id"),
            contract_hash=_required_text(value.get("contract_hash"), "contract_hash"),
            worktree_path=_required_text(value.get("worktree_path"), "worktree_path"),
            base_ref=_required_text(value.get("base_ref"), "base_ref"),
            deadline=deadline_value,
        )


@dataclass(frozen=True)
class RuntimePendingOutcome:
    correlation: RuntimeCorrelation
    status: RuntimePendingStatus
    approval_reason: str = ""


@dataclass(frozen=True)
class RuntimeTerminalOutcome:
    correlation: RuntimeCorrelation
    status: RuntimeTerminalStatus
    result: str
    error: str


@dataclass(frozen=True)
class RuntimeMissingOutcome:
    correlation: RuntimeCorrelation
    error: str


RuntimeOutcome = RuntimePendingOutcome | RuntimeTerminalOutcome | RuntimeMissingOutcome


_PENDING_STATUSES = {
    status.value: status
    for status in RuntimePendingStatus
    if status is not RuntimePendingStatus.TIMEOUT_PENDING
}
_TERMINAL_STATUSES = {status.value: status for status in RuntimeTerminalStatus}


class TaskRuntimeAdapter:
    """Submit and poll task_runtime work without owning orchestration state."""

    def __init__(
        self,
        task_runtime: TaskRuntimeModule,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._task_runtime = task_runtime
        self._clock = clock

    def submit(
        self,
        spec: TaskSpec,
        prompt: str,
        *,
        attempt_id: str,
        base_ref: str,
    ) -> RuntimeCorrelation:
        """Submit one isolated code task and return its polling correlation."""
        if not isinstance(spec, TaskSpec):
            raise TypeError("spec must be a TaskSpec")
        clean_prompt = _required_text(prompt, "prompt")
        clean_attempt_id = _required_text(attempt_id, "attempt_id")
        clean_base_ref = _required_text(base_ref, "base_ref")
        submitted_at = self._now()

        task = self._task_runtime.submit_task(
            clean_prompt,
            kind="code",
            source="orchestrator_loop",
            isolated_workspace=True,
            meta={
                "orchestrator_task_id": spec.task_id,
                "attempt_id": clean_attempt_id,
                "contract_sha256": spec.contract_hash,
                "base_ref": clean_base_ref,
            },
        )
        if not isinstance(task, Mapping):
            raise RuntimeProtocolError("task_runtime.submit_task returned a non-object")

        runtime_task_id = _mapping_text(task, "id")
        workspace = task.get("workspace")
        worktree_path = _validated_worktree_path(workspace)
        if worktree_path is None:
            cancellation_error = ""
            if runtime_task_id:
                try:
                    self._task_runtime.cancel_task(runtime_task_id)
                except Exception as exc:  # pragma: no cover - defensive fail-closed detail
                    cancellation_error = f"; cancellation failed: {type(exc).__name__}: {exc}"
            reason = _workspace_failure_reason(workspace)
            raise RuntimeIsolationError(
                f"isolated runtime workspace unavailable: {reason}{cancellation_error}",
                runtime_task_id=runtime_task_id,
            )
        if not runtime_task_id:
            raise RuntimeProtocolError("task_runtime submission is missing id")

        return RuntimeCorrelation(
            runtime_task_id=runtime_task_id,
            attempt_id=clean_attempt_id,
            contract_hash=spec.contract_hash,
            worktree_path=worktree_path,
            base_ref=clean_base_ref,
            deadline=submitted_at + spec.budget.wall_time_seconds,
        )

    def poll(self, correlation: RuntimeCorrelation) -> RuntimeOutcome:
        """Poll an existing task, requesting cancellation once after its deadline."""
        if not isinstance(correlation, RuntimeCorrelation):
            raise TypeError("correlation must be a RuntimeCorrelation")

        task = self._task_runtime.get_task(correlation.runtime_task_id)
        if task is None:
            return RuntimeMissingOutcome(
                correlation=correlation,
                error=f"runtime task not found: {correlation.runtime_task_id}",
            )
        if not isinstance(task, Mapping):
            raise RuntimeProtocolError("task_runtime.get_task returned a non-object")

        terminal = _terminal_outcome(correlation, task)
        if terminal is not None:
            return terminal

        if self._now() >= correlation.deadline:
            if not bool(task.get("cancel_requested")):
                cancelled = self._task_runtime.cancel_task(correlation.runtime_task_id)
                if isinstance(cancelled, Mapping):
                    terminal = _terminal_outcome(correlation, cancelled)
                    if terminal is not None:
                        return terminal
            return RuntimePendingOutcome(
                correlation=correlation,
                status=RuntimePendingStatus.TIMEOUT_PENDING,
            )

        raw_status = str(task.get("status") or "").strip()
        status = _PENDING_STATUSES.get(raw_status)
        if status is None:
            raise RuntimeProtocolError(f"unknown task_runtime status: {raw_status or '<empty>'}")
        return RuntimePendingOutcome(
            correlation=correlation,
            status=status,
            approval_reason=(
                str(task.get("approval_reason") or "")
                if status is RuntimePendingStatus.WAITING_APPROVAL
                else ""
            ),
        )

    def _now(self) -> float:
        now = self._clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise TypeError("clock must return an epoch number")
        value = float(now)
        if not math.isfinite(value):
            raise ValueError("clock must return a finite epoch number")
        return value


RuntimeAdapter = TaskRuntimeAdapter


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty, trimmed string")
    return value


def _mapping_text(value: Mapping[str, Any], key: str) -> str:
    raw = value.get(key)
    return raw.strip() if isinstance(raw, str) else ""


def _validated_worktree_path(workspace: Any) -> str | None:
    if not isinstance(workspace, Mapping):
        return None
    path = workspace.get("worktree_path")
    if workspace.get("enabled") is not True or workspace.get("ok") is not True:
        return None
    if not isinstance(path, str) or not path.strip():
        return None
    return path.strip()


def _workspace_failure_reason(workspace: Any) -> str:
    if not isinstance(workspace, Mapping):
        return "workspace metadata missing"
    reason = workspace.get("reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    if workspace.get("enabled") is not True:
        return "workspace not enabled"
    if workspace.get("ok") is not True:
        return "workspace preparation failed"
    return "worktree path missing"


def _terminal_outcome(
    correlation: RuntimeCorrelation,
    task: Mapping[str, Any],
) -> RuntimeTerminalOutcome | None:
    raw_status = str(task.get("status") or "").strip()
    status = _TERMINAL_STATUSES.get(raw_status)
    if status is None:
        return None
    return RuntimeTerminalOutcome(
        correlation=correlation,
        status=status,
        result=str(task.get("result") or ""),
        error=str(task.get("error") or ""),
    )
