from __future__ import annotations

from pathlib import Path

import pytest

from harness.task_contract import (
    AttemptRecord,
    AttemptStore,
    Capability,
    ContractError,
    OutputSpec,
    SideEffect,
    TaskContract,
    TaskSpec,
    TaskType,
    approval_logged,
    contract_for_task,
    evaluate_completion,
    load_contracts,
    normalized_task_spec_digest,
    save_contracts,
    task_contract_digest,
    validate_contract,
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


def test_serialized_legacy_contract_preserves_identity_and_hash():
    original = TaskSpec.from_queue_task(
        {"task": "Run the focused regression suite", "notes": "Capture failures"}
    )

    reparsed = TaskSpec.from_queue_task(original.to_dict())

    assert reparsed.task_id == original.task_id
    assert reparsed.legacy_adapter is True
    assert reparsed.contract_hash == original.contract_hash


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


def test_normalized_task_spec_digest_ignores_legacy_adapter_transition():
    legacy = TaskSpec.from_queue_task(
        {"task": "Run focused checks", "notes": "Capture failures"}
    )

    assert legacy.legacy_adapter is True
    assert normalized_task_spec_digest(legacy) == normalized_task_spec_digest(
        legacy.for_dispatch()
    )
    assert legacy.task_spec_hash != normalized_task_spec_digest(legacy)


def test_normalized_task_spec_digest_ignores_queue_state():
    awaiting = {**_task(), "status": "awaiting_approval", "blocked_at": "now"}
    queued = {**_task(), "status": "queued"}

    assert normalized_task_spec_digest(awaiting) == normalized_task_spec_digest(queued)


@pytest.mark.parametrize(
    "task_id", ["../escape", "bad/id", "bad id", "bad\x1b[2J", "x" * 129]
)
def test_task_spec_rejects_unsafe_explicit_task_ids(task_id):
    with pytest.raises(ContractError, match="task_id"):
        TaskSpec.from_queue_task(_task(id=task_id))


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


# ── Typed TaskContract (CODEX-8) ──────────────────────────────────────────────


def _contract(**overrides) -> TaskContract:
    contract = TaskContract(
        task_id="jarvis-audit-memory-events-verify",
        task_type=TaskType.ANALYSIS,
        description="Verify audit.jsonl captures memory_write events end-to-end",
        task_spec_sha256="a" * 64,
        outputs=[
            OutputSpec(
                name="verification_report",
                type="file",
                path_template="logs/{task_id}_report.md",
            )
        ],
        side_effects=[SideEffect.WRITES_FILES, SideEffect.SUBPROCESS],
        requires_capabilities=[Capability.FILESYSTEM, Capability.PYTHON],
        gate_pre_commit=True,
        preconditions=["logs/audit.jsonl exists"],
        postconditions=["report written"],
    )
    for key, value in overrides.items():
        setattr(contract, key, value)
    return contract


def test_validate_contract_passes_for_valid_contract():
    is_valid, errors = validate_contract(_contract())

    assert is_valid is True
    assert errors == []


def test_validate_contract_rejects_empty_task_id():
    is_valid, errors = validate_contract(_contract(task_id="  "))

    assert is_valid is False
    assert any("task_id" in error for error in errors)


@pytest.mark.parametrize(
    "digest", ["", "a" * 63, "A" * 64, "g" * 64, f" {'a' * 64}", 1]
)
def test_validate_contract_requires_canonical_task_spec_digest(digest):
    is_valid, errors = validate_contract(_contract(task_spec_sha256=digest))

    assert is_valid is False
    assert any("task_spec_sha256" in error for error in errors)


def test_validate_contract_requires_outputs_when_writing_files():
    is_valid, errors = validate_contract(_contract(outputs=[]))

    assert is_valid is False
    assert any("writes_files" in error for error in errors)


def test_validate_contract_requires_pre_commit_gate_when_writing_files():
    is_valid, errors = validate_contract(_contract(gate_pre_commit=False))

    assert is_valid is False
    assert any("gate_pre_commit" in error for error in errors)


def test_validate_contract_requires_preconditions_for_approval_gated_tasks():
    is_valid, errors = validate_contract(
        _contract(requires_approval=True, preconditions=[])
    )

    assert is_valid is False
    assert any("requires_approval" in error for error in errors)


def test_contracts_save_load_round_trip(tmp_path: Path):
    path = tmp_path / "TASK_CONTRACTS.json"
    original = _contract()

    save_contracts({original.task_id: original}, path)
    loaded = load_contracts(path)

    assert set(loaded) == {original.task_id}
    reloaded = loaded[original.task_id]
    assert reloaded.task_type is TaskType.ANALYSIS
    assert reloaded.side_effects == [SideEffect.WRITES_FILES, SideEffect.SUBPROCESS]
    assert reloaded.requires_capabilities == [Capability.FILESYSTEM, Capability.PYTHON]
    assert reloaded.outputs[0].path_template == "logs/{task_id}_report.md"
    assert reloaded.task_spec_sha256 == "a" * 64
    assert reloaded.to_dict() == original.to_dict()


def test_task_contract_digest_changes_with_contract_content():
    original = _contract()
    changed = _contract(description="Changed approval scope")

    assert task_contract_digest(original) == original.contract_hash
    assert task_contract_digest(original) != task_contract_digest(changed)


def test_task_contract_digest_includes_task_spec_binding():
    original = _contract(task_spec_sha256="a" * 64)
    rebound = _contract(task_spec_sha256="b" * 64)

    assert original.contract_hash != rebound.contract_hash


def test_contract_for_task_returns_none_for_unknown_task(tmp_path: Path):
    path = tmp_path / "TASK_CONTRACTS.json"
    save_contracts({"known-task": _contract(task_id="known-task")}, path)

    assert contract_for_task("unknown-task", path) is None
    assert contract_for_task("known-task", path) is not None
    assert contract_for_task("", path) is None


def test_load_contracts_skips_invalid_entries(tmp_path: Path):
    path = tmp_path / "TASK_CONTRACTS.json"
    path.write_text(
        '[{"task_id": "bad-type", "task_type": "not-a-type", "description": "x"}]',
        encoding="utf-8",
    )

    assert load_contracts(path) == {}


def test_approval_logged_requires_exact_bound_digests(tmp_path: Path):
    path = tmp_path / "approved_tasks.json"
    contract_digest = "a" * 64
    spec_digest = "b" * 64
    path.write_text(
        '[{"task_id": "jarvis-local-llm-cross-session-memory",'
        f' "task_contract_sha256": "{contract_digest}",'
        f' "task_spec_sha256": "{spec_digest}",'
        ' "approved_at": "2026-07-04T00:00:00+00:00", "approved_by": "aman"}]',
        encoding="utf-8",
    )

    assert approval_logged("jarvis-local-llm-cross-session-memory", path) is False
    assert approval_logged(
        "jarvis-local-llm-cross-session-memory",
        path,
        task_contract_sha256=contract_digest,
    ) is False
    assert approval_logged(
        "jarvis-local-llm-cross-session-memory",
        path,
        task_contract_sha256=contract_digest,
        task_spec_sha256=spec_digest,
    ) is True
    assert approval_logged(
        "jarvis-local-llm-cross-session-memory",
        path,
        task_contract_sha256="c" * 64,
        task_spec_sha256=spec_digest,
    ) is False


def test_approval_logged_rejects_old_unbound_record(tmp_path: Path):
    path = tmp_path / "approved_tasks.json"
    path.write_text(
        '[{"task_id": "legacy-task", "approved_at": "2026-07-04T00:00:00+00:00",'
        ' "approved_by": "aman"}]',
        encoding="utf-8",
    )

    assert approval_logged(
        "legacy-task",
        path,
        task_contract_sha256="a" * 64,
        task_spec_sha256="b" * 64,
    ) is False
