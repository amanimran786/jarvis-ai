from __future__ import annotations

from pathlib import Path

import pytest

from harness.task_contract import (
    AttemptRecord,
    AttemptStore,
    ContractError,
    TaskSpec,
    evaluate_completion,
)


def _task(**overrides):
    task = {
        "id": "TASK-042",
        "title": "Add retry policy",
        "description": "Retry transient web failures.",
        "allowed_files": ["harness/web_search.py", "tests/test_web_search.py"],
        "forbidden_files": [".env"],
        "acceptance_criteria": ["429 responses retry three times"],
        "verification_commands": ["pytest tests/test_web_search.py -q"],
        "constraints": {"local_first": True, "network": False},
        "budget": {"max_attempts": 2, "wall_time_seconds": 900, "tool_calls": 20},
        "domain": "harness",
        "assigned_ai": "codex",
    }
    task.update(overrides)
    return task


def test_task_spec_preserves_enforced_contract_fields():
    spec = TaskSpec.from_queue_task(_task())

    assert spec.task_id == "TASK-042"
    assert spec.allowed_files == ("harness/web_search.py", "tests/test_web_search.py")
    assert spec.verification_commands == ("pytest tests/test_web_search.py -q",)
    assert spec.budget.max_attempts == 2
    assert spec.constraints["local_first"] is True
    assert spec.legacy_adapter is False


def test_legacy_task_gets_stable_compatibility_id():
    task = {"task": "Run the focused regression suite", "notes": "Capture failures"}

    first = TaskSpec.from_queue_task(task)
    second = TaskSpec.from_queue_task(task)

    assert first.task_id == second.task_id
    assert first.task_id.startswith("LEGACY-")
    assert first.legacy_adapter is True


@pytest.mark.parametrize("unsafe", ["/tmp/file.py", "../secret", "src/../../secret"])
def test_task_spec_rejects_paths_outside_worktree(unsafe):
    with pytest.raises(ContractError, match="unsafe path"):
        TaskSpec.from_queue_task(_task(allowed_files=[unsafe]))


def test_contract_hash_changes_when_acceptance_contract_changes():
    original = TaskSpec.from_queue_task(_task())
    changed = TaskSpec.from_queue_task(
        _task(acceptance_criteria=["429 responses retry four times"])
    )

    assert original.contract_hash != changed.contract_hash


def test_task_spec_rejects_non_serializable_constraints():
    with pytest.raises(ContractError, match="JSON-serializable"):
        TaskSpec.from_queue_task(_task(constraints={"callback": object()}))


def test_attempt_store_persists_dispatch_checkpoint(tmp_path: Path):
    spec = TaskSpec.from_queue_task(_task())
    record = AttemptRecord.dispatched(spec, "jarvis-harness-codex-task042")
    store = AttemptStore(tmp_path / "attempts.jsonl")

    store.append(record)
    saved = store.read_all()

    assert saved[0]["attempt_id"] == record.attempt_id
    assert saved[0]["phase"] == "dispatch"
    assert saved[0]["contract_sha256"] == spec.contract_hash
    assert saved[0]["remaining_budget"]["tool_calls"] == 20


def _evidence(**overrides):
    evidence = {
        "observer": "loop",
        "changed_files": ["harness/web_search.py", "tests/test_web_search.py"],
        "commands": [
            {"command": "pytest tests/test_web_search.py -q", "exit_code": 0}
        ],
        "policy_findings": [],
    }
    evidence.update(overrides)
    return evidence


def test_completion_requires_loop_observed_evidence():
    spec = TaskSpec.from_queue_task(_task())

    verdict = evaluate_completion(spec, {"observer": "agent", "changed_files": []})

    assert verdict.status == "unverified"
    assert verdict.failure_class == "verification_missing"


def test_completion_accepts_matching_scope_and_successful_commands():
    spec = TaskSpec.from_queue_task(_task())

    verdict = evaluate_completion(spec, _evidence())

    assert verdict.passed is True
    assert verdict.status == "verified"


def test_completion_rejects_file_outside_allowed_scope():
    spec = TaskSpec.from_queue_task(_task())

    verdict = evaluate_completion(spec, _evidence(changed_files=["router.py"]))

    assert verdict.status == "rejected"
    assert verdict.failure_class == "scope_violation"


def test_completion_rejects_failed_verification_command():
    spec = TaskSpec.from_queue_task(_task())
    evidence = _evidence(
        commands=[
            {"command": "pytest tests/test_web_search.py -q", "exit_code": 1}
        ]
    )

    verdict = evaluate_completion(spec, evidence)

    assert verdict.status == "rejected"
    assert verdict.failure_class == "test_failure"


def test_completion_rejects_unresolved_policy_findings():
    spec = TaskSpec.from_queue_task(_task())

    verdict = evaluate_completion(
        spec,
        _evidence(policy_findings=[{"id": "SEC-1", "status": "open"}]),
    )

    assert verdict.status == "rejected"
    assert verdict.failure_class == "policy_failure"
