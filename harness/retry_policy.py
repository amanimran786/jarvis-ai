"""Deterministic, loop-owned retry and escalation decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from harness.task_contract import TaskBudget


class FailureClass(str, Enum):
    """Failure categories understood by the orchestration loop."""

    TEST_FAILURE = "test_failure"
    AGENT_ERROR = "agent_error"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    VERIFICATION_MISSING = "verification_missing"
    SCOPE_VIOLATION = "scope_violation"
    POLICY_FAILURE = "policy_failure"
    CONTRACT_MISMATCH = "contract_mismatch"
    UNKNOWN = "unknown"


class RetryAction(str, Enum):
    """Next transition selected by the loop."""

    RETRY = "retry"
    ROUTE_TO_VERIFIER = "route_to_verifier"
    ESCALATE = "escalate"


class RetryTarget(str, Enum):
    """Role that should receive the next transition."""

    EXECUTOR = "executor"
    VERIFIER = "verifier"
    HUMAN = "human"


@dataclass(frozen=True)
class RetryDecision:
    """A typed transition that can be persisted without parsing prose."""

    action: RetryAction
    target: RetryTarget
    failure_class: FailureClass
    attempt_number: int
    remaining_attempts: int
    reason: str

    @property
    def should_retry(self) -> bool:
        return self.action is RetryAction.RETRY

    @property
    def should_escalate(self) -> bool:
        return self.action is RetryAction.ESCALATE


_RETRYABLE_FAILURES = frozenset(
    {
        FailureClass.TEST_FAILURE,
        FailureClass.AGENT_ERROR,
        FailureClass.INFRASTRUCTURE_FAILURE,
    }
)

_NON_RETRYABLE_FAILURES = frozenset(
    {
        FailureClass.SCOPE_VIOLATION,
        FailureClass.POLICY_FAILURE,
        FailureClass.CONTRACT_MISMATCH,
    }
)


def _failure_class(value: FailureClass | str) -> FailureClass:
    if isinstance(value, FailureClass):
        return value
    try:
        return FailureClass(str(value).strip().lower())
    except ValueError:
        return FailureClass.UNKNOWN


def _validate_inputs(
    attempt_number: int,
    budget: TaskBudget,
    remaining_wall_time_seconds: int,
    remaining_tool_calls: int,
) -> None:
    if not isinstance(budget, TaskBudget):
        raise TypeError("budget must be a TaskBudget")
    if attempt_number < 1:
        raise ValueError("attempt_number must be at least 1")
    if remaining_wall_time_seconds < 0:
        raise ValueError("remaining_wall_time_seconds cannot be negative")
    if remaining_tool_calls < 0:
        raise ValueError("remaining_tool_calls cannot be negative")


def decide_retry(
    failure_class: FailureClass | str,
    attempt_number: int,
    budget: TaskBudget,
    remaining_wall_time_seconds: int,
    remaining_tool_calls: int,
) -> RetryDecision:
    """Choose the loop's next transition for a classified failure.

    ``attempt_number`` is the one-based number of the attempt that just ended.
    Verifier routing does not consume another executor attempt, but still requires
    wall-time and tool-call budget because it launches additional work.
    """
    _validate_inputs(
        attempt_number,
        budget,
        remaining_wall_time_seconds,
        remaining_tool_calls,
    )
    classified = _failure_class(failure_class)
    remaining_attempts = max(0, budget.max_attempts - attempt_number)

    def decision(
        action: RetryAction,
        target: RetryTarget,
        reason: str,
    ) -> RetryDecision:
        return RetryDecision(
            action=action,
            target=target,
            failure_class=classified,
            attempt_number=attempt_number,
            remaining_attempts=remaining_attempts,
            reason=reason,
        )

    if classified in _NON_RETRYABLE_FAILURES:
        return decision(
            RetryAction.ESCALATE,
            RetryTarget.HUMAN,
            f"{classified.value} requires escalation",
        )

    if remaining_wall_time_seconds == 0:
        return decision(
            RetryAction.ESCALATE,
            RetryTarget.HUMAN,
            "wall-time budget exhausted",
        )
    if remaining_tool_calls == 0:
        return decision(
            RetryAction.ESCALATE,
            RetryTarget.HUMAN,
            "tool-call budget exhausted",
        )

    if classified is FailureClass.VERIFICATION_MISSING:
        return decision(
            RetryAction.ROUTE_TO_VERIFIER,
            RetryTarget.VERIFIER,
            "independent verification evidence is required",
        )

    if classified in _RETRYABLE_FAILURES:
        if remaining_attempts == 0:
            return decision(
                RetryAction.ESCALATE,
                RetryTarget.HUMAN,
                "attempt budget exhausted",
            )
        return decision(
            RetryAction.RETRY,
            RetryTarget.EXECUTOR,
            f"{classified.value} is retryable within the remaining budget",
        )

    return decision(
        RetryAction.ESCALATE,
        RetryTarget.HUMAN,
        "unknown failure class requires escalation",
    )
