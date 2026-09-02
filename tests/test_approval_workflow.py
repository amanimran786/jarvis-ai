from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

import jarvis_cli
from harness.approval_workflow import (
    ApprovalWorkflowError,
    consume_approval,
    list_pending_approvals,
    record_approval,
    requeue_approved_task,
    restore_approval,
)
from harness.task_contract import (
    TaskContract,
    TaskSpec,
    TaskType,
    approval_logged,
    normalized_task_spec_digest,
    save_contracts,
    task_contract_digest,
)


def _contract(task_id: str, *, requires_approval: bool = True) -> TaskContract:
    return TaskContract(
        task_id=task_id,
        task_type=TaskType.ANALYSIS,
        description=f"Approval-gated task {task_id}",
        requires_approval=requires_approval,
        preconditions=["human approval is recorded"] if requires_approval else [],
    )


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    return (
        tmp_path / "TASK_CONTRACTS.json",
        tmp_path / "WORK_QUEUE.json",
        tmp_path / "approved_tasks.json",
    )


def _queue_task(task_id: str, **overrides) -> dict:
    task = {
        "contract_id": task_id,
        "task": f"Execute {task_id}",
        "notes": "Use the approved scope",
        "status": "awaiting_approval",
    }
    task.update(overrides)
    return task


def _write_queue(path: Path, *tasks: dict) -> None:
    path.write_text(json.dumps(list(tasks)), encoding="utf-8")


def test_record_approval_validates_contract_exists(tmp_path: Path) -> None:
    contracts_path, queue_path, approvals_path = _paths(tmp_path)
    save_contracts({}, contracts_path)
    _write_queue(queue_path, _queue_task("missing-task"))

    with pytest.raises(ApprovalWorkflowError, match="no task contract found"):
        record_approval(
            "missing-task",
            queue_path=queue_path,
            contracts_path=contracts_path,
            approvals_path=approvals_path,
        )

    assert not approvals_path.exists()


def test_record_approval_writes_utc_record_atomically(tmp_path: Path) -> None:
    contracts_path, queue_path, approvals_path = _paths(tmp_path)
    contract = _contract("sensitive-task")
    save_contracts({contract.task_id: contract}, contracts_path)
    task = _queue_task(contract.task_id)
    _write_queue(queue_path, task)

    record, created = record_approval(
        contract.task_id,
        approved_by="aman",
        queue_path=queue_path,
        contracts_path=contracts_path,
        approvals_path=approvals_path,
    )

    assert created is True
    assert json.loads(approvals_path.read_text(encoding="utf-8")) == [record]
    assert record["approved_by"] == "aman"
    assert record["task_contract_sha256"] == task_contract_digest(contract)
    assert record["task_spec_sha256"] == normalized_task_spec_digest(task)
    assert datetime.fromisoformat(record["approved_at"]).utcoffset().total_seconds() == 0
    assert approvals_path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob(".approved_tasks.json.*.tmp")) == []


def test_record_approval_deduplicates_exact_binding(tmp_path: Path) -> None:
    contracts_path, queue_path, approvals_path = _paths(tmp_path)
    contract = _contract("sensitive-task")
    save_contracts({contract.task_id: contract}, contracts_path)
    _write_queue(queue_path, _queue_task(contract.task_id))
    duplicate, _ = record_approval(
        contract.task_id,
        approved_by="aman",
        approved_at="2026-07-05T01:00:00+00:00",
        queue_path=queue_path,
        contracts_path=contracts_path,
        approvals_path=approvals_path,
    )
    approvals_path.write_text(json.dumps([duplicate, dict(duplicate)]), encoding="utf-8")

    record, created = record_approval(
        contract.task_id,
        approved_by="other-user",
        queue_path=queue_path,
        contracts_path=contracts_path,
        approvals_path=approvals_path,
    )

    assert created is False
    assert record == duplicate
    assert json.loads(approvals_path.read_text(encoding="utf-8")) == [duplicate]


def test_record_approval_rejects_malformed_existing_state(tmp_path: Path) -> None:
    contracts_path, queue_path, approvals_path = _paths(tmp_path)
    contract = _contract("sensitive-task")
    save_contracts({contract.task_id: contract}, contracts_path)
    _write_queue(queue_path, _queue_task(contract.task_id))
    approvals_path.write_text(
        json.dumps([{"task_id": contract.task_id}]), encoding="utf-8"
    )

    with pytest.raises(ApprovalWorkflowError, match="approved_at, approved_by"):
        record_approval(
            contract.task_id,
            queue_path=queue_path,
            contracts_path=contracts_path,
            approvals_path=approvals_path,
        )

    assert json.loads(approvals_path.read_text(encoding="utf-8")) == [
        {"task_id": contract.task_id}
    ]


def test_atomic_replace_failure_preserves_existing_approvals(tmp_path: Path) -> None:
    contracts_path, queue_path, approvals_path = _paths(tmp_path)
    first = _contract("first-task")
    second = _contract("second-task")
    save_contracts({first.task_id: first, second.task_id: second}, contracts_path)
    _write_queue(queue_path, _queue_task(first.task_id), _queue_task(second.task_id))
    original = [
        {
            "task_id": first.task_id,
            "approved_at": "2026-07-05T01:00:00+00:00",
            "approved_by": "aman",
        }
    ]
    approvals_path.write_text(json.dumps(original), encoding="utf-8")

    with patch("harness.approval_workflow.os.replace", side_effect=OSError("replace failed")):
        with pytest.raises(OSError, match="replace failed"):
            record_approval(
                second.task_id,
                approved_by="aman",
                queue_path=queue_path,
                contracts_path=contracts_path,
                approvals_path=approvals_path,
            )

    assert json.loads(approvals_path.read_text(encoding="utf-8")) == original
    assert list(tmp_path.glob(".approved_tasks.json.*.tmp")) == []


def test_list_pending_approvals_merges_queue_and_contracts(tmp_path: Path) -> None:
    contracts_path, queue_path, approvals_path = _paths(tmp_path)
    queued = _contract("queued-task")
    contract_only = _contract("contract-only")
    approved = _contract("approved-task")
    not_gated = _contract("not-gated", requires_approval=False)
    contracts = {item.task_id: item for item in (queued, contract_only, approved, not_gated)}
    save_contracts(contracts, contracts_path)
    queue_path.write_text(
        json.dumps(
            [
                {
                    "contract_id": queued.task_id,
                    "task": "Queued approval task",
                    "status": "awaiting_approval",
                },
                {
                    "contract_id": approved.task_id,
                    "task": "Approved but not dispatched yet",
                    "status": "awaiting_approval",
                },
                {"contract_id": "ignored", "status": "queued"},
            ]
        ),
        encoding="utf-8",
    )
    record_approval(
        approved.task_id,
        approved_by="aman",
        queue_path=queue_path,
        contracts_path=contracts_path,
        approvals_path=approvals_path,
    )

    pending = list_pending_approvals(
        queue_path=queue_path,
        contracts_path=contracts_path,
        approvals_path=approvals_path,
    )
    by_id = {item["task_id"]: item for item in pending}

    assert set(by_id) == {"approved-task", "contract-only", "queued-task"}
    assert by_id["queued-task"]["sources"] == ["work_queue", "contract"]
    assert by_id["contract-only"]["sources"] == ["contract"]
    assert by_id["approved-task"]["approval_logged"] is True


def test_changed_contract_and_spec_invalidate_approval(tmp_path: Path) -> None:
    contracts_path, queue_path, approvals_path = _paths(tmp_path)
    contract = _contract("sensitive-task")
    task = _queue_task(contract.task_id)
    save_contracts({contract.task_id: contract}, contracts_path)
    _write_queue(queue_path, task)
    record, _ = record_approval(
        contract.task_id,
        queue_path=queue_path,
        contracts_path=contracts_path,
        approvals_path=approvals_path,
    )

    changed_contract = _contract(contract.task_id)
    changed_contract.description = "Mutated contract"
    changed_spec = TaskSpec.from_queue_task({**task, "notes": "Mutated task"})

    assert approval_logged(
        contract.task_id,
        approvals_path,
        task_contract_sha256=task_contract_digest(changed_contract),
        task_spec_sha256=record["task_spec_sha256"],
    ) is False
    assert approval_logged(
        contract.task_id,
        approvals_path,
        task_contract_sha256=record["task_contract_sha256"],
        task_spec_sha256=normalized_task_spec_digest(changed_spec),
    ) is False


def test_record_approval_does_not_dedupe_changed_spec(tmp_path: Path) -> None:
    contracts_path, queue_path, approvals_path = _paths(tmp_path)
    contract = _contract("sensitive-task")
    save_contracts({contract.task_id: contract}, contracts_path)
    _write_queue(queue_path, _queue_task(contract.task_id, notes="first"))
    first, first_created = record_approval(
        contract.task_id,
        queue_path=queue_path,
        contracts_path=contracts_path,
        approvals_path=approvals_path,
    )
    _write_queue(queue_path, _queue_task(contract.task_id, notes="second"))

    second, second_created = record_approval(
        contract.task_id,
        queue_path=queue_path,
        contracts_path=contracts_path,
        approvals_path=approvals_path,
    )

    assert first_created is True
    assert second_created is True
    assert first["task_spec_sha256"] != second["task_spec_sha256"]
    assert len(json.loads(approvals_path.read_text(encoding="utf-8"))) == 2


def test_consume_approval_is_single_use_and_preserves_permissions(tmp_path: Path) -> None:
    contracts_path, queue_path, approvals_path = _paths(tmp_path)
    contract = _contract("sensitive-task")
    save_contracts({contract.task_id: contract}, contracts_path)
    _write_queue(queue_path, _queue_task(contract.task_id))
    record, _ = record_approval(
        contract.task_id,
        queue_path=queue_path,
        contracts_path=contracts_path,
        approvals_path=approvals_path,
    )
    approvals_path.write_text(json.dumps([record, dict(record)]), encoding="utf-8")

    rejected = consume_approval(
        contract.task_id,
        task_contract_sha256="c" * 64,
        task_spec_sha256=record["task_spec_sha256"],
        approvals_path=approvals_path,
    )
    consumed = consume_approval(
        contract.task_id,
        task_contract_sha256=record["task_contract_sha256"],
        task_spec_sha256=record["task_spec_sha256"],
        approvals_path=approvals_path,
    )
    consumed_again = consume_approval(
        contract.task_id,
        task_contract_sha256=record["task_contract_sha256"],
        task_spec_sha256=record["task_spec_sha256"],
        approvals_path=approvals_path,
    )

    assert rejected is None
    assert consumed == record
    assert consumed_again is None
    assert json.loads(approvals_path.read_text(encoding="utf-8")) == []
    assert approvals_path.stat().st_mode & 0o777 == 0o600


def test_restore_approval_reinserts_exact_consumed_record(tmp_path: Path) -> None:
    contracts_path, queue_path, approvals_path = _paths(tmp_path)
    contract = _contract("sensitive-task")
    save_contracts({contract.task_id: contract}, contracts_path)
    _write_queue(queue_path, _queue_task(contract.task_id))
    record, _ = record_approval(
        contract.task_id,
        approved_by="aman",
        queue_path=queue_path,
        contracts_path=contracts_path,
        approvals_path=approvals_path,
    )
    consumed = consume_approval(
        contract.task_id,
        task_contract_sha256=record["task_contract_sha256"],
        task_spec_sha256=record["task_spec_sha256"],
        approvals_path=approvals_path,
    )

    restored = restore_approval(consumed, approvals_path=approvals_path)

    assert restored is True
    assert json.loads(approvals_path.read_text(encoding="utf-8")) == [record]
    assert approvals_path.stat().st_mode & 0o777 == 0o600


def test_restore_approval_does_not_duplicate_live_binding(tmp_path: Path) -> None:
    contracts_path, queue_path, approvals_path = _paths(tmp_path)
    contract = _contract("sensitive-task")
    save_contracts({contract.task_id: contract}, contracts_path)
    _write_queue(queue_path, _queue_task(contract.task_id))
    record, _ = record_approval(
        contract.task_id,
        queue_path=queue_path,
        contracts_path=contracts_path,
        approvals_path=approvals_path,
    )

    same_binding = {
        **record,
        "approved_at": "2026-07-12T12:05:00+00:00",
        "approved_by": "other-user",
    }
    restored = restore_approval(same_binding, approvals_path=approvals_path)

    assert restored is False
    assert json.loads(approvals_path.read_text(encoding="utf-8")) == [record]


@pytest.mark.parametrize(
    "invalid_update",
    [
        pytest.param(lambda record: None, id="not-an-object"),
        pytest.param(
            lambda record: {key: value for key, value in record.items() if key != "approved_by"},
            id="missing-field",
        ),
        pytest.param(
            lambda record: {**record, "task_id": " spaced-task "},
            id="noncanonical-task-id",
        ),
        pytest.param(
            lambda record: {**record, "task_contract_sha256": "A" * 64},
            id="invalid-contract-digest",
        ),
        pytest.param(
            lambda record: {**record, "task_spec_sha256": "not-a-digest"},
            id="invalid-spec-digest",
        ),
        pytest.param(
            lambda record: {**record, "task_spec_sha256": f" {'b' * 64} "},
            id="noncanonical-spec-digest",
        ),
        pytest.param(
            lambda record: {**record, "approved_at": ""},
            id="empty-approved-at",
        ),
    ],
)
def test_restore_approval_rejects_invalid_records_without_writing(
    tmp_path: Path, invalid_update
) -> None:
    _, _, approvals_path = _paths(tmp_path)
    record = {
        "task_id": "sensitive-task",
        "task_contract_sha256": "a" * 64,
        "task_spec_sha256": "b" * 64,
        "approved_at": "2026-07-12T12:00:00+00:00",
        "approved_by": "aman",
    }
    approvals_path.write_text("[]\n", encoding="utf-8")

    with pytest.raises(ApprovalWorkflowError, match="invalid approval record"):
        restore_approval(invalid_update(record), approvals_path=approvals_path)

    assert json.loads(approvals_path.read_text(encoding="utf-8")) == []


def test_restore_approval_replace_failure_preserves_existing_state(tmp_path: Path) -> None:
    _, _, approvals_path = _paths(tmp_path)
    existing = {
        "task_id": "first-task",
        "task_contract_sha256": "a" * 64,
        "task_spec_sha256": "b" * 64,
        "approved_at": "2026-07-12T12:00:00+00:00",
        "approved_by": "aman",
    }
    restored = {
        "task_id": "second-task",
        "task_contract_sha256": "c" * 64,
        "task_spec_sha256": "d" * 64,
        "approved_at": "2026-07-12T12:01:00+00:00",
        "approved_by": "aman",
    }
    approvals_path.write_text(json.dumps([existing]), encoding="utf-8")

    with patch("harness.approval_workflow.os.replace", side_effect=OSError("replace failed")):
        with pytest.raises(OSError, match="replace failed"):
            restore_approval(restored, approvals_path=approvals_path)

    assert json.loads(approvals_path.read_text(encoding="utf-8")) == [existing]
    assert list(tmp_path.glob(".approved_tasks.json.*.tmp")) == []


def test_requeue_approved_task_releases_matching_awaiting_row(tmp_path: Path) -> None:
    _, queue_path, _ = _paths(tmp_path)
    queue_path.write_text(
        json.dumps(
            [
                {
                    "contract_id": "approved-task",
                    "status": "awaiting_approval",
                    "blocked_reason": "requires human approval before execution",
                    "blocked_at": "2026-07-05T01:00:00+00:00",
                },
                {"contract_id": "other-task", "status": "awaiting_approval"},
            ]
        ),
        encoding="utf-8",
    )

    changed = requeue_approved_task("approved-task", queue_path=queue_path)

    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    assert changed is True
    assert queue[0]["status"] == "queued"
    assert "blocked_reason" not in queue[0]
    assert "blocked_at" not in queue[0]
    assert queue[1]["status"] == "awaiting_approval"
    assert queue_path.stat().st_mode & 0o777 == 0o600


def test_approve_command_routes_contract_ids_to_contract_workflow() -> None:
    with (
        patch.object(jarvis_cli, "contract_for_task", return_value=object()),
        patch.object(jarvis_cli, "_approve_contract_task", return_value=0) as contract_approve,
        patch.object(jarvis_cli, "approve_task") as managed_approve,
    ):
        result = jarvis_cli._handle_console_command("/approve contract-task")

    assert result == 0
    contract_approve.assert_called_once_with("contract-task")
    managed_approve.assert_not_called()


def test_approve_command_preserves_managed_task_fallback() -> None:
    with (
        patch.object(jarvis_cli, "contract_for_task", return_value=None),
        patch.object(jarvis_cli, "approve_task", return_value=0) as managed_approve,
        patch.object(jarvis_cli, "_approve_contract_task") as contract_approve,
    ):
        result = jarvis_cli._handle_console_command("/approve managed-task")

    assert result == 0
    managed_approve.assert_called_once_with("managed-task")
    contract_approve.assert_not_called()


def test_approve_without_args_preserves_pending_shell_behavior() -> None:
    with patch.object(
        jarvis_cli, "_approve_pending_shell_command", return_value=0
    ) as shell_approve:
        result = jarvis_cli._handle_console_command("/approve")

    assert result == 0
    shell_approve.assert_called_once_with()


def test_pending_approval_command_dispatches_locally() -> None:
    with patch.object(
        jarvis_cli, "_print_pending_approvals", return_value=0
    ) as print_pending:
        result = jarvis_cli._handle_console_command("/pending-approval")

    assert result == 0
    print_pending.assert_called_once_with()


def test_pending_approval_output_strips_terminal_controls(capsys) -> None:
    pending = [
        {
            "task_id": "safe-id\x1b[2J",
            "status": "awaiting_approval",
            "description": "review\x1b]0;spoof\x07 now",
            "approval_logged": False,
        }
    ]
    with patch.object(jarvis_cli, "list_pending_approvals", return_value=pending):
        result = jarvis_cli._print_pending_approvals()

    rendered = capsys.readouterr().out
    assert result == 0
    assert "\x1b" not in rendered


def test_list_pending_approvals_skips_queued_task_already_approved(tmp_path: Path) -> None:
    """A recorded digest-bound approval satisfies the gate for a queued task."""
    contracts_path, queue_path, approvals_path = _paths(tmp_path)
    gated = _contract("gated-task")
    save_contracts({gated.task_id: gated}, contracts_path)
    queue_path.write_text(
        json.dumps(
            [
                {
                    "contract_id": gated.task_id,
                    "task": "Waiting to be dispatched",
                    "status": "queued",
                }
            ]
        ),
        encoding="utf-8",
    )

    before = list_pending_approvals(
        queue_path=queue_path,
        contracts_path=contracts_path,
        approvals_path=approvals_path,
    )
    assert [item["task_id"] for item in before] == [gated.task_id]

    record_approval(
        gated.task_id,
        approved_by="aman",
        queue_path=queue_path,
        contracts_path=contracts_path,
        approvals_path=approvals_path,
    )

    after = list_pending_approvals(
        queue_path=queue_path,
        contracts_path=contracts_path,
        approvals_path=approvals_path,
    )
    assert after == []
