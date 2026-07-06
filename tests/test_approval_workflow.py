from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

import jarvis_cli
from harness.approval_workflow import (
    ApprovalWorkflowError,
    list_pending_approvals,
    record_approval,
    requeue_approved_task,
)
from harness.task_contract import TaskContract, TaskType, save_contracts


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


def test_record_approval_validates_contract_exists(tmp_path: Path) -> None:
    contracts_path, _, approvals_path = _paths(tmp_path)
    save_contracts({}, contracts_path)

    with pytest.raises(ApprovalWorkflowError, match="no task contract found"):
        record_approval(
            "missing-task",
            contracts_path=contracts_path,
            approvals_path=approvals_path,
        )

    assert not approvals_path.exists()


def test_record_approval_writes_utc_record_atomically(tmp_path: Path) -> None:
    contracts_path, _, approvals_path = _paths(tmp_path)
    contract = _contract("sensitive-task")
    save_contracts({contract.task_id: contract}, contracts_path)

    record, created = record_approval(
        contract.task_id,
        approved_by="aman",
        contracts_path=contracts_path,
        approvals_path=approvals_path,
    )

    assert created is True
    assert json.loads(approvals_path.read_text(encoding="utf-8")) == [record]
    assert record["approved_by"] == "aman"
    assert datetime.fromisoformat(record["approved_at"]).utcoffset().total_seconds() == 0
    assert approvals_path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob(".approved_tasks.json.*.tmp")) == []


def test_record_approval_deduplicates_by_task_id(tmp_path: Path) -> None:
    contracts_path, _, approvals_path = _paths(tmp_path)
    contract = _contract("sensitive-task")
    save_contracts({contract.task_id: contract}, contracts_path)
    duplicate = {
        "task_id": contract.task_id,
        "approved_at": "2026-07-05T01:00:00+00:00",
        "approved_by": "aman",
    }
    approvals_path.write_text(json.dumps([duplicate, dict(duplicate)]), encoding="utf-8")

    record, created = record_approval(
        contract.task_id,
        approved_by="other-user",
        contracts_path=contracts_path,
        approvals_path=approvals_path,
    )

    assert created is False
    assert record == duplicate
    assert json.loads(approvals_path.read_text(encoding="utf-8")) == [duplicate]


def test_record_approval_rejects_malformed_existing_state(tmp_path: Path) -> None:
    contracts_path, _, approvals_path = _paths(tmp_path)
    contract = _contract("sensitive-task")
    save_contracts({contract.task_id: contract}, contracts_path)
    approvals_path.write_text(
        json.dumps([{"task_id": contract.task_id}]), encoding="utf-8"
    )

    with pytest.raises(ApprovalWorkflowError, match="approved_at, approved_by"):
        record_approval(
            contract.task_id,
            contracts_path=contracts_path,
            approvals_path=approvals_path,
        )

    assert json.loads(approvals_path.read_text(encoding="utf-8")) == [
        {"task_id": contract.task_id}
    ]


def test_atomic_replace_failure_preserves_existing_approvals(tmp_path: Path) -> None:
    contracts_path, _, approvals_path = _paths(tmp_path)
    first = _contract("first-task")
    second = _contract("second-task")
    save_contracts({first.task_id: first, second.task_id: second}, contracts_path)
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
    approvals_path.write_text(
        json.dumps(
            [
                {
                    "task_id": approved.task_id,
                    "approved_at": "2026-07-05T01:00:00+00:00",
                    "approved_by": "aman",
                }
            ]
        ),
        encoding="utf-8",
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
