from __future__ import annotations

import pytest

from harness.retry_policy import (
    FailureClass,
    RetryAction,
    RetryTarget,
    decide_retry,
)
from harness.task_contract import TaskBudget


BUDGET = TaskBudget(max_attempts=3, wall_time_seconds=600, tool_calls=20)


@pytest.mark.parametrize(
    "failure_class",
    [
        FailureClass.TEST_FAILURE,
        FailureClass.AGENT_ERROR,
        FailureClass.INFRASTRUCTURE_FAILURE,
    ],
)
def test_retryable_failures_retry_within_budget(failure_class):
    decision = decide_retry(failure_class, 1, BUDGET, 300, 10)

    assert decision.action is RetryAction.RETRY
    assert decision.target is RetryTarget.EXECUTOR
    assert decision.should_retry is True
    assert decision.remaining_attempts == 2


@pytest.mark.parametrize(
    ("attempt_number", "remaining_wall", "remaining_tools", "reason"),
    [
        (3, 300, 10, "attempt budget exhausted"),
        (1, 0, 10, "wall-time budget exhausted"),
        (1, 300, 0, "tool-call budget exhausted"),
    ],
)
def test_retryable_failure_escalates_when_a_budget_is_exhausted(
    attempt_number, remaining_wall, remaining_tools, reason
):
    decision = decide_retry(
        FailureClass.TEST_FAILURE,
        attempt_number,
        BUDGET,
        remaining_wall,
        remaining_tools,
    )

    assert decision.action is RetryAction.ESCALATE
    assert decision.target is RetryTarget.HUMAN
    assert decision.should_retry is False
    assert decision.reason == reason


def test_verification_missing_routes_to_verifier_without_executor_retry():
    decision = decide_retry("verification_missing", 3, BUDGET, 300, 10)

    assert decision.action is RetryAction.ROUTE_TO_VERIFIER
    assert decision.target is RetryTarget.VERIFIER
    assert decision.should_retry is False
    assert decision.remaining_attempts == 0


def test_verifier_route_escalates_when_operating_budget_is_exhausted():
    decision = decide_retry("verification_missing", 1, BUDGET, 0, 10)

    assert decision.action is RetryAction.ESCALATE
    assert decision.reason == "wall-time budget exhausted"


@pytest.mark.parametrize(
    "failure_class",
    [
        FailureClass.SCOPE_VIOLATION,
        FailureClass.POLICY_FAILURE,
        FailureClass.CONTRACT_MISMATCH,
    ],
)
def test_contract_and_policy_failures_escalate_without_retry(failure_class):
    decision = decide_retry(failure_class, 1, BUDGET, 300, 10)

    assert decision.action is RetryAction.ESCALATE
    assert decision.target is RetryTarget.HUMAN
    assert decision.should_escalate is True
    assert failure_class.value in decision.reason


def test_unknown_failure_class_fails_closed_to_escalation():
    decision = decide_retry("surprising_new_failure", 1, BUDGET, 300, 10)

    assert decision.failure_class is FailureClass.UNKNOWN
    assert decision.action is RetryAction.ESCALATE


@pytest.mark.parametrize(
    ("attempt_number", "remaining_wall", "remaining_tools", "message"),
    [
        (0, 300, 10, "attempt_number"),
        (1, -1, 10, "remaining_wall_time_seconds"),
        (1, 300, -1, "remaining_tool_calls"),
    ],
)
def test_invalid_budget_state_is_rejected(
    attempt_number, remaining_wall, remaining_tools, message
):
    with pytest.raises(ValueError, match=message):
        decide_retry(
            FailureClass.TEST_FAILURE,
            attempt_number,
            BUDGET,
            remaining_wall,
            remaining_tools,
        )
