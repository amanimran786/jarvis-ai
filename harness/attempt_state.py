"""Deterministic lifecycle state for one orchestration attempt."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


class AttemptState(str, Enum):
    """Canonical states owned by the agent loop."""

    QUEUED = "queued"
    DISPATCH_PENDING = "dispatch_pending"
    HANDOFF_READY = "handoff_ready"
    RUNNING = "running"
    COMPLETION_CLAIMED = "completion_claimed"
    VERIFYING = "verifying"
    RETRY_PENDING = "retry_pending"
    VERIFIED = "verified"
    BLOCKED = "blocked"
    FAILED = "failed"


TERMINAL_STATES = frozenset(
    {
        AttemptState.RETRY_PENDING,
        AttemptState.VERIFIED,
        AttemptState.BLOCKED,
        AttemptState.FAILED,
    }
)

_ACTIVE_FAILURE_TARGETS = frozenset({AttemptState.BLOCKED, AttemptState.FAILED})
_ALLOWED_TRANSITIONS = {
    AttemptState.QUEUED: frozenset({AttemptState.DISPATCH_PENDING}) | _ACTIVE_FAILURE_TARGETS,
    AttemptState.DISPATCH_PENDING: frozenset({AttemptState.HANDOFF_READY, AttemptState.RETRY_PENDING})
    | _ACTIVE_FAILURE_TARGETS,
    AttemptState.HANDOFF_READY: frozenset({AttemptState.RUNNING, AttemptState.RETRY_PENDING})
    | _ACTIVE_FAILURE_TARGETS,
    AttemptState.RUNNING: frozenset({AttemptState.COMPLETION_CLAIMED, AttemptState.RETRY_PENDING})
    | _ACTIVE_FAILURE_TARGETS,
    AttemptState.COMPLETION_CLAIMED: frozenset({AttemptState.VERIFYING, AttemptState.RETRY_PENDING})
    | _ACTIVE_FAILURE_TARGETS,
    AttemptState.VERIFYING: frozenset({AttemptState.VERIFIED, AttemptState.RETRY_PENDING})
    | _ACTIVE_FAILURE_TARGETS,
    AttemptState.RETRY_PENDING: frozenset(),
    AttemptState.VERIFIED: frozenset(),
    AttemptState.BLOCKED: frozenset(),
    AttemptState.FAILED: frozenset(),
}


class AttemptStateError(ValueError):
    """Base error for invalid lifecycle construction or movement."""


class IllegalTransitionError(AttemptStateError):
    """Raised when an attempt cannot move between the requested states."""


class TerminalStateError(IllegalTransitionError):
    """Raised when code tries to mutate a terminal attempt."""


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise AttemptStateError(f"{field_name} must be a non-empty, trimmed string")
    return value


@dataclass(frozen=True)
class TransitionRecord:
    """Immutable, JSON-safe evidence of one accepted state transition."""

    sequence: int
    attempt_id: str
    task_id: str
    contract_hash: str
    from_state: AttemptState
    to_state: AttemptState
    reason: str = ""
    next_attempt_number: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "attempt_id": self.attempt_id,
            "task_id": self.task_id,
            "contract_hash": self.contract_hash,
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
            "reason": self.reason,
            "next_attempt_number": self.next_attempt_number,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


class AttemptLifecycle:
    """Validate and record state transitions for a single attempt."""

    def __init__(self, *, attempt_id: str, task_id: str, contract_hash: str) -> None:
        self.attempt_id = _required_text(attempt_id, "attempt_id")
        self.task_id = _required_text(task_id, "task_id")
        self.contract_hash = _required_text(contract_hash, "contract_hash")
        self._state = AttemptState.QUEUED
        self._records: list[TransitionRecord] = []

    @property
    def state(self) -> AttemptState:
        return self._state

    @property
    def is_terminal(self) -> bool:
        return self._state in TERMINAL_STATES

    @property
    def records(self) -> tuple[TransitionRecord, ...]:
        return tuple(self._records)

    @property
    def allowed_transitions(self) -> frozenset[AttemptState]:
        return _ALLOWED_TRANSITIONS[self._state]

    def transition(
        self,
        to_state: AttemptState,
        *,
        reason: str = "",
        next_attempt_number: int | None = None,
    ) -> TransitionRecord:
        """Move to an allowed state and append its immutable transition record."""
        if not isinstance(to_state, AttemptState):
            raise TypeError("to_state must be an AttemptState")
        if not isinstance(reason, str):
            raise TypeError("reason must be a string")
        if next_attempt_number is not None:
            if isinstance(next_attempt_number, bool) or not isinstance(next_attempt_number, int):
                raise TypeError("next_attempt_number must be an integer")
            if next_attempt_number < 1:
                raise AttemptStateError("next_attempt_number must be at least 1")
            if to_state is not AttemptState.RETRY_PENDING:
                raise AttemptStateError(
                    "next_attempt_number is only valid for retry_pending transitions"
                )
        if self.is_terminal:
            raise TerminalStateError(f"terminal state {self._state.value} is immutable")
        if to_state not in self.allowed_transitions:
            raise IllegalTransitionError(
                f"illegal attempt transition: {self._state.value} -> {to_state.value}"
            )

        record = TransitionRecord(
            sequence=len(self._records) + 1,
            attempt_id=self.attempt_id,
            task_id=self.task_id,
            contract_hash=self.contract_hash,
            from_state=self._state,
            to_state=to_state,
            reason=reason,
            next_attempt_number=next_attempt_number,
        )
        self._records.append(record)
        self._state = to_state
        return record

    def records_to_json(self) -> str:
        """Serialize the append-only transition log without adding persistence policy."""
        return json.dumps(
            [record.to_dict() for record in self._records],
            sort_keys=True,
            separators=(",", ":"),
        )
