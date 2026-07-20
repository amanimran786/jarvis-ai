from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from harness.attempt_state import (
    TERMINAL_STATES,
    AttemptLifecycle,
    AttemptState,
    AttemptStateError,
    IllegalTransitionError,
    TerminalStateError,
)


ATTEMPT_ID = "attempt_001"
TASK_ID = "TASK-001"
CONTRACT_HASH = "a" * 64

CANONICAL_PATH = (
    AttemptState.DISPATCH_PENDING,
    AttemptState.HANDOFF_READY,
    AttemptState.RUNNING,
    AttemptState.COMPLETION_CLAIMED,
    AttemptState.VERIFYING,
    AttemptState.VERIFIED,
)


def _lifecycle() -> AttemptLifecycle:
    return AttemptLifecycle(
        attempt_id=ATTEMPT_ID,
        task_id=TASK_ID,
        contract_hash=CONTRACT_HASH,
    )


def _advance(lifecycle: AttemptLifecycle, *states: AttemptState) -> None:
    for state in states:
        lifecycle.transition(state)


def test_attempt_states_are_stable_string_values():
    assert [state.value for state in AttemptState] == [
        "queued",
        "dispatch_pending",
        "handoff_ready",
        "running",
        "completion_claimed",
        "verifying",
        "retry_pending",
        "verified",
        "blocked",
        "failed",
    ]
    assert TERMINAL_STATES == {
        AttemptState.RETRY_PENDING,
        AttemptState.VERIFIED,
        AttemptState.BLOCKED,
        AttemptState.FAILED,
    }


@pytest.mark.parametrize("field_name", ["attempt_id", "task_id", "contract_hash"])
@pytest.mark.parametrize("invalid", ["", "   ", " value "])
def test_lifecycle_requires_non_empty_trimmed_identity_fields(field_name, invalid):
    values = {
        "attempt_id": ATTEMPT_ID,
        "task_id": TASK_ID,
        "contract_hash": CONTRACT_HASH,
    }
    values[field_name] = invalid

    with pytest.raises(AttemptStateError, match=field_name):
        AttemptLifecycle(**values)


@pytest.mark.parametrize("field_name", ["attempt_id", "task_id", "contract_hash"])
def test_lifecycle_rejects_non_string_identity_fields(field_name):
    values = {
        "attempt_id": ATTEMPT_ID,
        "task_id": TASK_ID,
        "contract_hash": CONTRACT_HASH,
    }
    values[field_name] = None

    with pytest.raises(TypeError, match=field_name):
        AttemptLifecycle(**values)


def test_canonical_success_path_records_every_transition_in_order():
    lifecycle = _lifecycle()

    _advance(lifecycle, *CANONICAL_PATH)

    assert lifecycle.state is AttemptState.VERIFIED
    assert lifecycle.is_terminal is True
    assert [record.sequence for record in lifecycle.records] == list(range(1, 7))
    assert [record.from_state for record in lifecycle.records] == [
        AttemptState.QUEUED,
        *CANONICAL_PATH[:-1],
    ]
    assert [record.to_state for record in lifecycle.records] == list(CANONICAL_PATH)


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ((), {AttemptState.DISPATCH_PENDING, AttemptState.BLOCKED, AttemptState.FAILED}),
        (
            (AttemptState.DISPATCH_PENDING,),
            {AttemptState.HANDOFF_READY, AttemptState.RETRY_PENDING, AttemptState.BLOCKED, AttemptState.FAILED},
        ),
        (
            (AttemptState.DISPATCH_PENDING, AttemptState.HANDOFF_READY),
            {AttemptState.RUNNING, AttemptState.RETRY_PENDING, AttemptState.BLOCKED, AttemptState.FAILED},
        ),
        (
            (AttemptState.DISPATCH_PENDING, AttemptState.HANDOFF_READY, AttemptState.RUNNING),
            {
                AttemptState.COMPLETION_CLAIMED,
                AttemptState.RETRY_PENDING,
                AttemptState.BLOCKED,
                AttemptState.FAILED,
            },
        ),
        (
            (
                AttemptState.DISPATCH_PENDING,
                AttemptState.HANDOFF_READY,
                AttemptState.RUNNING,
                AttemptState.COMPLETION_CLAIMED,
            ),
            {AttemptState.VERIFYING, AttemptState.RETRY_PENDING, AttemptState.BLOCKED, AttemptState.FAILED},
        ),
        (
            (
                AttemptState.DISPATCH_PENDING,
                AttemptState.HANDOFF_READY,
                AttemptState.RUNNING,
                AttemptState.COMPLETION_CLAIMED,
                AttemptState.VERIFYING,
            ),
            {AttemptState.VERIFIED, AttemptState.RETRY_PENDING, AttemptState.BLOCKED, AttemptState.FAILED},
        ),
    ],
)
def test_each_active_state_exposes_only_its_canonical_transitions(prefix, expected):
    lifecycle = _lifecycle()
    _advance(lifecycle, *prefix)

    assert lifecycle.allowed_transitions == expected


@pytest.mark.parametrize("source_path", [(), CANONICAL_PATH[:2], CANONICAL_PATH[:4]])
def test_illegal_transition_is_rejected_without_changing_state_or_history(source_path):
    lifecycle = _lifecycle()
    _advance(lifecycle, *source_path)
    original_state = lifecycle.state
    original_records = lifecycle.records

    with pytest.raises(IllegalTransitionError, match="illegal attempt transition"):
        lifecycle.transition(original_state)

    assert lifecycle.state is original_state
    assert lifecycle.records == original_records


@pytest.mark.parametrize("retry_from", [1, 2, 3, 4, 5])
def test_retry_pending_ends_the_current_attempt(retry_from):
    lifecycle = _lifecycle()
    _advance(lifecycle, *CANONICAL_PATH[:retry_from])

    retry = lifecycle.transition(AttemptState.RETRY_PENDING, reason="retryable failure")

    assert lifecycle.state is AttemptState.RETRY_PENDING
    assert lifecycle.is_terminal is True
    assert retry.to_state is AttemptState.RETRY_PENDING
    assert retry.reason == "retryable failure"
    with pytest.raises(TerminalStateError, match="immutable"):
        lifecycle.transition(AttemptState.DISPATCH_PENDING)


def test_retry_handoff_can_reference_next_attempt_number_without_creating_its_identity():
    lifecycle = _lifecycle()
    _advance(lifecycle, *CANONICAL_PATH[:3])

    retry = lifecycle.transition(
        AttemptState.RETRY_PENDING,
        reason="executor failed",
        next_attempt_number=2,
    )

    assert retry.attempt_id == ATTEMPT_ID
    assert retry.next_attempt_number == 2
    assert retry.to_dict()["next_attempt_number"] == 2
    assert not hasattr(retry, "next_attempt_id")
    assert lifecycle.attempt_id == ATTEMPT_ID
    assert lifecycle.state is AttemptState.RETRY_PENDING


@pytest.mark.parametrize("invalid", [0, -1])
def test_retry_handoff_rejects_non_positive_next_attempt_number(invalid):
    lifecycle = _lifecycle()
    _advance(lifecycle, *CANONICAL_PATH[:3])

    with pytest.raises(AttemptStateError, match="at least 1"):
        lifecycle.transition(AttemptState.RETRY_PENDING, next_attempt_number=invalid)


@pytest.mark.parametrize("invalid", [True, 2.0, "2"])
def test_retry_handoff_rejects_non_integer_next_attempt_number(invalid):
    lifecycle = _lifecycle()
    _advance(lifecycle, *CANONICAL_PATH[:3])

    with pytest.raises(TypeError, match="integer"):
        lifecycle.transition(AttemptState.RETRY_PENDING, next_attempt_number=invalid)


def test_next_attempt_number_is_rejected_for_non_retry_transition():
    lifecycle = _lifecycle()

    with pytest.raises(AttemptStateError, match="only valid for retry_pending"):
        lifecycle.transition(AttemptState.DISPATCH_PENDING, next_attempt_number=2)

    assert lifecycle.state is AttemptState.QUEUED
    assert lifecycle.records == ()


@pytest.mark.parametrize("terminal", list(TERMINAL_STATES))
def test_terminal_states_are_immutable(terminal):
    lifecycle = _lifecycle()
    if terminal is AttemptState.VERIFIED:
        _advance(lifecycle, *CANONICAL_PATH)
    elif terminal is AttemptState.RETRY_PENDING:
        _advance(lifecycle, *CANONICAL_PATH[:3])
        lifecycle.transition(terminal)
    else:
        lifecycle.transition(terminal)
    original_records = lifecycle.records

    with pytest.raises(TerminalStateError, match="immutable"):
        lifecycle.transition(AttemptState.FAILED)

    assert lifecycle.state is terminal
    assert lifecycle.records == original_records
    assert lifecycle.allowed_transitions == frozenset()


def test_transition_requires_typed_state_and_string_reason():
    lifecycle = _lifecycle()

    with pytest.raises(TypeError, match="AttemptState"):
        lifecycle.transition("dispatch_pending")
    with pytest.raises(TypeError, match="reason"):
        lifecycle.transition(AttemptState.DISPATCH_PENDING, reason=None)

    assert lifecycle.state is AttemptState.QUEUED
    assert lifecycle.records == ()


def test_records_are_frozen_and_history_is_exposed_as_a_tuple():
    lifecycle = _lifecycle()
    record = lifecycle.transition(AttemptState.DISPATCH_PENDING)

    with pytest.raises(FrozenInstanceError):
        record.reason = "changed"

    assert lifecycle.records == (record,)
    with pytest.raises(AttributeError):
        lifecycle.records.append(record)


def test_transition_record_and_log_are_json_serializable_and_deterministic():
    lifecycle = _lifecycle()
    record = lifecycle.transition(AttemptState.DISPATCH_PENDING, reason="worker requested")
    expected = {
        "sequence": 1,
        "attempt_id": ATTEMPT_ID,
        "task_id": TASK_ID,
        "contract_hash": CONTRACT_HASH,
        "from_state": "queued",
        "to_state": "dispatch_pending",
        "reason": "worker requested",
        "next_attempt_number": None,
    }

    assert record.to_dict() == expected
    assert json.loads(record.to_json()) == expected
    assert json.loads(lifecycle.records_to_json()) == [expected]
    assert lifecycle.records_to_json() == lifecycle.records_to_json()
