from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import orchestrator_loop
from harness.task_contract import TaskContract, TaskSpec, TaskType


def _explicit_task(**overrides: Any) -> dict[str, Any]:
    task = {
        "id": "AUTH-001",
        "title": "Produce an authorization report",
        "description": "Write the approved authorization findings.",
        "goal": "Produce the exact report authorized by the operator.",
        "status": "queued",
        "priority": 1,
        "allowed_files": ["reports/authorization.md"],
        "acceptance_criteria": ["The report contains the approved findings"],
        "verification_commands": [],
        "constraints": {"local_first": True, "network": False},
        "domain": "orchestrator",
        "assigned_ai": "codex",
    }
    task.update(overrides)
    return task


def _contract(
    task_id: str = "AUTH-001", task: dict[str, Any] | None = None
) -> TaskContract:
    bound_spec = TaskSpec.from_queue_task(task or _explicit_task()).for_dispatch()
    return TaskContract(
        task_id=task_id,
        task_type=TaskType.ANALYSIS,
        description="Authorize one exact report task.",
        task_spec_sha256=bound_spec.normalized_task_spec_hash,
        requires_approval=True,
        preconditions=["Operator approval matches the contract and task digests"],
    )


@pytest.fixture
def loop_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    queue_path = tmp_path / "WORK_QUEUE.json"
    contracts: dict[str, TaskContract] = {}
    approvals: list[dict[str, str]] = []
    launches: list[dict[str, Any]] = []
    attempts: list[Any] = []
    active_sessions: dict[str, str] = {}
    checkpoint_failure: list[bool] = []
    launch_delivery_result = ["written"]

    class FakeSessionTracker:
        def list_completed(self) -> list[dict[str, Any]]:
            return []

        def purge_completed(self, _session_ids=None) -> None:
            return None

        def active_count(self) -> int:
            return len(active_sessions)

        def claim(self, task_id: str, session_id: str, **_: Any) -> None:
            active_sessions[session_id] = task_id

    class FakeAttemptStore:
        def __init__(self, _path: Path) -> None:
            pass

        def append(self, attempt: Any) -> None:
            if checkpoint_failure:
                raise OSError("simulated checkpoint failure")
            attempts.append(attempt)

    def approval_logged(
        task_id: str,
        *,
        task_contract_sha256: str = "",
        task_spec_sha256: str = "",
        **_: Any,
    ) -> bool:
        return any(
            record.get("task_id") == task_id
            and record.get("task_contract_sha256") == task_contract_sha256
            and record.get("task_spec_sha256") == task_spec_sha256
            for record in approvals
        )

    def consume_approval(
        task_id: str,
        *,
        task_contract_sha256: str = "",
        task_spec_sha256: str = "",
        **_: Any,
    ) -> dict[str, str] | None:
        for index, record in enumerate(approvals):
            if (
                record.get("task_id") == task_id
                and record.get("task_contract_sha256") == task_contract_sha256
                and record.get("task_spec_sha256") == task_spec_sha256
            ):
                return approvals.pop(index)
        return None

    def restore_approval(record: dict[str, str], **_: Any) -> bool:
        if record in approvals:
            return False
        approvals.append(record)
        return True

    def write_launch_record(record: dict[str, Any]) -> str:
        result = launch_delivery_result[0]
        if result == "written":
            launches.append(record)
        return result

    monkeypatch.setattr(orchestrator_loop, "WORK_QUEUE_PATH", queue_path)
    monkeypatch.setattr(orchestrator_loop, "LAUNCH_QUEUE_PATH", tmp_path / "LAUNCH_QUEUE.json")
    monkeypatch.setattr(orchestrator_loop, "MASTER_LOG_PATH", tmp_path / "MASTER_LOG.md")
    monkeypatch.setattr(orchestrator_loop, "ATTEMPT_LOG_PATH", tmp_path / "attempts.jsonl")
    monkeypatch.setattr(orchestrator_loop, "SessionTracker", FakeSessionTracker)
    monkeypatch.setattr(orchestrator_loop, "AttemptStore", FakeAttemptStore)
    monkeypatch.setattr(orchestrator_loop, "_ensure_dashboard_running", lambda: None)
    monkeypatch.setattr(orchestrator_loop, "_expire_stalled_sessions", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(orchestrator_loop, "_build_repo_context", lambda: {"repo": "test"})
    monkeypatch.setattr(orchestrator_loop, "_git_head", lambda: "test-base-ref")
    monkeypatch.setattr(orchestrator_loop, "_write_master_lines", lambda _lines: None)
    monkeypatch.setattr(orchestrator_loop, "_now", lambda: "2026-07-12T12:00:00+00:00")
    monkeypatch.setattr(
        orchestrator_loop,
        "_session_id_for_task",
        lambda task: f"test-session-{task['id'].lower()}",
    )
    monkeypatch.setattr(
        orchestrator_loop,
        "contract_for_task",
        lambda task_id: contracts.get(task_id),
    )
    monkeypatch.setattr(orchestrator_loop, "check_contract_capabilities", lambda _contract: {})
    monkeypatch.setattr(orchestrator_loop, "approval_logged", approval_logged)
    monkeypatch.setattr(orchestrator_loop, "consume_approval", consume_approval, raising=False)
    monkeypatch.setattr(orchestrator_loop, "restore_approval", restore_approval)
    monkeypatch.setattr(orchestrator_loop, "_write_launch_record", write_launch_record)
    monkeypatch.setattr(
        "harness.prompt_generator.generate_session_prompt",
        lambda _task, _repo: "test authorization prompt",
    )

    class Harness:
        def seed(self, task: dict[str, Any]) -> None:
            queue_path.write_text(json.dumps([task]), encoding="utf-8")

        def queue_task(self) -> dict[str, Any]:
            return json.loads(queue_path.read_text(encoding="utf-8"))[0]

        def approve(self, task: dict[str, Any], contract: TaskContract) -> None:
            spec = TaskSpec.from_queue_task(task)
            approvals.append(
                {
                    "task_id": contract.task_id,
                    "task_contract_sha256": contract.contract_hash,
                    "task_spec_sha256": spec.normalized_task_spec_hash,
                }
            )

        def run(self) -> dict[str, Any]:
            return orchestrator_loop.run_loop(max_concurrent=1, dry_run=False)

    harness = Harness()
    harness.contracts = contracts
    harness.approvals = approvals
    harness.launches = launches
    harness.attempts = attempts
    harness.active_sessions = active_sessions
    harness.checkpoint_failure = checkpoint_failure
    harness.launch_delivery_result = launch_delivery_result
    return harness


def test_legacy_loop_leaves_uncontracted_task_for_codex(loop_harness) -> None:
    task = _explicit_task()
    loop_harness.seed(task)

    result = loop_harness.run()

    assert result["launched"] == 0
    assert result["blocked"] == 0
    assert loop_harness.launches == []
    assert loop_harness.queue_task() == task


def test_legacy_loop_does_not_consume_digest_bound_approval(loop_harness) -> None:
    task = _explicit_task()
    contract = _contract()
    loop_harness.contracts[contract.task_id] = contract
    loop_harness.approve(task, contract)
    approval = dict(loop_harness.approvals[0])
    loop_harness.seed(task)

    result = loop_harness.run()

    assert result["launched"] == 0
    assert result["blocked"] == 0
    assert loop_harness.launches == []
    assert loop_harness.approvals == [approval]
    assert loop_harness.queue_task() == task


@pytest.mark.parametrize("status", ["queued", "awaiting_approval", "proposed"])
def test_legacy_loop_never_dispatches_engineering_statuses(
    loop_harness,
    status: str,
) -> None:
    task = _explicit_task(status=status)
    loop_harness.seed(task)

    result = loop_harness.run()

    assert result["launched"] == 0
    assert loop_harness.launches == []
    assert loop_harness.queue_task() == task
