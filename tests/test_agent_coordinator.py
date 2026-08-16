from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest

from harness import agent_coordinator
from harness.agent_coordinator import (
    CoordinationError,
    assign_task,
    claim_next,
    finish,
    heartbeat,
    review_completion,
    set_cooldown,
    status_snapshot,
)
from harness.approval_workflow import record_approval
from harness.task_contract import (
    OutputSpec,
    SideEffect,
    TaskContract,
    TaskType,
    normalized_task_spec_digest,
    task_contract_digest,
)


UTC = dt.timezone.utc


def test_runtime_queue_lock_is_gitignored():
    repo_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        ["git", "check-ignore", "--quiet", ".jarvis-work-queue.lock"],
        cwd=repo_root,
        check=False,
        shell=False,
        timeout=30,
    )

    assert result.returncode == 0


def _task(
    task_id: str,
    *,
    priority: int = 1,
    assigned_ai: str = "claude",
    status: str = "proposed",
) -> dict:
    return {
        "id": task_id,
        "contract_id": task_id,
        "title": f"Implement {task_id}",
        "goal": f"Produce {task_id} artifact",
        "description": f"Produce {task_id} artifact",
        "allowed_files": ["artifact.txt"],
        "verification_commands": [],
        "constraints": {"local_first": True},
        "budget": {
            "max_attempts": 2,
            "wall_time_seconds": 30,
            "tool_calls": 10,
        },
        "domain": "general",
        "assigned_ai": assigned_ai,
        "legacy_adapter": False,
        "priority": priority,
        "status": status,
        "assigned_to": None,
        "created_at": "2026-07-16T00:00:00+00:00",
    }


def _contract(task: dict, *, requires_approval: bool = False) -> TaskContract:
    return TaskContract(
        task_id=task["contract_id"],
        task_type=TaskType.CODE,
        description=task["description"],
        task_spec_sha256=normalized_task_spec_digest(task),
        outputs=[
            OutputSpec(
                name="artifact",
                type="file",
                path_template="artifact.txt",
            )
        ],
        side_effects=[SideEffect.WRITES_FILES, SideEffect.MODIFIES_GIT],
        requires_approval=requires_approval,
        gate_pre_commit=True,
        preconditions=["operator approval recorded"] if requires_approval else [],
        postconditions=["artifact exists"],
    )


def _state_files(
    tmp_path: Path,
    tasks: list[dict],
    *,
    approval_ids: set[str] | None = None,
) -> dict[str, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    queue_path = tmp_path / "WORK_QUEUE.json"
    contracts_path = tmp_path / "TASK_CONTRACTS.json"
    approvals_path = tmp_path / "approved_tasks.json"
    state_path = tmp_path / "agent_coordination.json"
    approval_ids = approval_ids or set()
    queue_path.write_text(json.dumps(tasks), encoding="utf-8")
    contracts = [
        _contract(task, requires_approval=task["contract_id"] in approval_ids).to_dict()
        for task in tasks
    ]
    contracts_path.write_text(json.dumps(contracts), encoding="utf-8")
    approvals_path.write_text("[]\n", encoding="utf-8")
    return {
        "queue_path": queue_path,
        "contracts_path": contracts_path,
        "approvals_path": approvals_path,
        "state_path": state_path,
    }


def _claim(paths: dict[str, Path], agent: str, **kwargs):
    return claim_next(
        agent,
        repo_path=None,
        base_ref="a" * 40,
        **paths,
        **kwargs,
    )


def _assign(
    paths: dict[str, Path],
    task_id: str,
    worker: str,
    *,
    stage: str = "implementation",
    repo_path: Path | None = None,
    base_ref: str = "a" * 40,
    **kwargs,
):
    return assign_task(
        task_id,
        worker,
        stage=stage,
        rationale="Selected by Codex for focused verification",
        queue_path=paths["queue_path"],
        contracts_path=paths["contracts_path"],
        state_path=paths["state_path"],
        repo_path=repo_path,
        base_ref=base_ref,
        **kwargs,
    )


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        shell=False,
        timeout=30,
    )
    return result.stdout.strip()


def test_claim_requires_exact_codex_assignment_and_records_lease(tmp_path: Path):
    tasks = [_task("claude-task"), _task("codex-task")]
    paths = _state_files(tmp_path, tasks)
    _assign(paths, "claude-task", "claude")

    wrong_worker = _claim(paths, "codex")
    result = _claim(paths, "claude")

    assert wrong_worker["status"] == "idle"
    assert result["status"] == "claimed"
    assert result["task_id"] == "claude-task"
    queue = json.loads(paths["queue_path"].read_text())
    claimed = next(task for task in queue if task["contract_id"] == "claude-task")
    assert claimed["status"] == "in_progress"
    assert claimed["lease_owner"] == "claude"
    assert claimed["lease_id"] == result["lease_id"]


def test_cooldown_blocks_agent_claim(tmp_path: Path):
    paths = _state_files(tmp_path, [_task("shared-task")])
    now = dt.datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    _assign(paths, "shared-task", "claude", now=now)
    set_cooldown("claude", seconds=600, reason="rate limit", now=now, **{
        "queue_path": paths["queue_path"],
        "state_path": paths["state_path"],
    })

    result = _claim(paths, "claude", now=now + dt.timedelta(seconds=1))

    assert result["status"] == "cooldown"
    assert result["cooldown_until"] == "2026-07-16T12:10:00+00:00"


def test_cooling_worker_cannot_be_taken_over_without_codex_reassignment(tmp_path: Path):
    paths = _state_files(tmp_path, [_task("claude-task")])
    now = dt.datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    _assign(paths, "claude-task", "claude", now=now)

    assert _claim(paths, "codex", now=now)["status"] == "idle"

    set_cooldown("claude", seconds=600, reason="credit exhausted", now=now, **{
        "queue_path": paths["queue_path"],
        "state_path": paths["state_path"],
    })
    still_idle = _claim(
        paths,
        "codex",
        now=now + dt.timedelta(seconds=1),
    )
    _assign(
        paths,
        "claude-task",
        "codex",
        now=now + dt.timedelta(seconds=2),
    )
    result = _claim(paths, "codex", now=now + dt.timedelta(seconds=3))

    assert still_idle["status"] == "idle"
    assert result["status"] == "claimed"
    assert result["task_id"] == "claude-task"


def test_approval_task_waits_for_exact_digest_bound_approval(tmp_path: Path):
    task = _task("approval-task")
    paths = _state_files(tmp_path, [task], approval_ids={"approval-task"})
    _assign(paths, "approval-task", "codex")

    first = _claim(paths, "codex")

    assert first["status"] == "idle"
    queue = json.loads(paths["queue_path"].read_text())
    assert queue[0]["status"] == "awaiting_approval"

    record_approval(
        "approval-task",
        approved_by="test",
        queue_path=paths["queue_path"],
        contracts_path=paths["contracts_path"],
        approvals_path=paths["approvals_path"],
    )
    second = _claim(paths, "codex")

    assert second["status"] == "claimed"
    assert json.loads(paths["approvals_path"].read_text()) == []


def test_one_active_shared_checkout_lease_blocks_another_claim(tmp_path: Path):
    paths = _state_files(
        tmp_path,
        [_task("first-task"), _task("second-task")],
    )
    _assign(paths, "first-task", "codex")
    first = _claim(paths, "codex")

    second = _claim(paths, "claude")

    assert first["status"] == "claimed"
    assert second["status"] == "capacity"
    assert second["task_ids"] == ["first-task"]


def test_cooldown_releases_owned_lease_for_codex_reassignment(tmp_path: Path):
    paths = _state_files(tmp_path, [_task("shared-task")])
    now = dt.datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    _assign(paths, "shared-task", "claude", now=now)
    first = _claim(paths, "claude", now=now)

    cooldown = set_cooldown(
        "claude",
        seconds=600,
        reason="session limit",
        now=now + dt.timedelta(seconds=10),
        queue_path=paths["queue_path"],
        state_path=paths["state_path"],
    )
    assert _claim(paths, "codex", now=now + dt.timedelta(seconds=11))["status"] == "idle"
    _assign(paths, "shared-task", "codex", now=now + dt.timedelta(seconds=12))
    second = _claim(paths, "codex", now=now + dt.timedelta(seconds=13))

    assert cooldown["released_tasks"] == ["shared-task"]
    assert second["status"] == "claimed"
    assert second["task_id"] == first["task_id"]


def test_expired_lease_starts_owner_cooldown_and_requires_reassignment(tmp_path: Path):
    paths = _state_files(tmp_path, [_task("shared-task")])
    now = dt.datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    _assign(paths, "shared-task", "claude", now=now)
    _claim(paths, "claude", now=now, lease_seconds=10)

    expired = _claim(
        paths,
        "codex",
        now=now + dt.timedelta(seconds=11),
    )
    _assign(paths, "shared-task", "codex", now=now + dt.timedelta(seconds=12))
    result = _claim(paths, "codex", now=now + dt.timedelta(seconds=13))

    assert result["status"] == "claimed"
    assert expired["status"] == "idle"
    assert expired["expired_leases"] == ["shared-task"]
    snapshot = status_snapshot(
        queue_path=paths["queue_path"],
        state_path=paths["state_path"],
    )
    assert snapshot["coordination"]["agents"]["claude"]["status"] == "cooldown"


def test_heartbeat_rejects_wrong_agent(tmp_path: Path):
    paths = _state_files(tmp_path, [_task("shared-task")])
    _assign(paths, "shared-task", "claude")
    claim = _claim(paths, "claude")

    with pytest.raises(CoordinationError, match="ownership"):
        heartbeat(
            "codex",
            claim["task_id"],
            claim["lease_id"],
            queue_path=paths["queue_path"],
            state_path=paths["state_path"],
        )


def test_finish_waits_for_codex_review_before_marking_done(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Coordinator Test")
    (repo / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", ".gitignore", "base.txt")
    _git(repo, "commit", "-m", "base")

    paths = _state_files(tmp_path / "state", [_task("shared-task")])
    _assign(paths, "shared-task", "claude", repo_path=repo)
    claim = claim_next("claude", repo_path=repo, **paths)
    (repo / "artifact.txt").write_text("done\n")
    _git(repo, "add", "artifact.txt")
    _git(repo, "commit", "-m", "add artifact")

    result = finish(
        "claude",
        claim["task_id"],
        claim["lease_id"],
        summary="artifact complete",
        repo_path=repo,
        queue_path=paths["queue_path"],
        contracts_path=paths["contracts_path"],
        state_path=paths["state_path"],
    )

    assert result["status"] == "awaiting_codex_review"
    queue = json.loads(paths["queue_path"].read_text())
    assert queue[0]["status"] == "awaiting_codex_review"
    assert queue[0]["executed_by"] == "claude"
    assert "completed_by" not in queue[0]
    assert queue[0]["completion_commit"] == _git(repo, "rev-parse", "HEAD")
    assert queue[0]["completion_evidence"]["changed_files"] == ["artifact.txt"]

    reviewed = review_completion(
        "shared-task",
        decision="accept",
        summary="Codex accepted verified artifact",
        repo_path=repo,
        queue_path=paths["queue_path"],
        contracts_path=paths["contracts_path"],
        state_path=paths["state_path"],
    )

    assert reviewed["status"] == "done"
    queue = json.loads(paths["queue_path"].read_text())
    assert queue[0]["status"] == "done"
    assert queue[0]["completed_by"] == "codex"


def test_finish_blocks_committed_pre_commit_violation(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Coordinator Test")
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "base.txt")
    _git(repo, "commit", "-m", "base")

    task = _task("shared-task")
    task["allowed_files"] = ["artifact.txt", "tool.py"]
    paths = _state_files(tmp_path / "state", [task])
    _assign(paths, "shared-task", "codex", repo_path=repo)
    claim = claim_next("codex", repo_path=repo, **paths)
    unsafe = "run(command, shell" + "=True)\n"
    (repo / "tool.py").write_text(unsafe, encoding="utf-8")
    _git(repo, "add", "tool.py")
    _git(repo, "commit", "-m", "unsafe tool")

    result = finish(
        "codex",
        claim["task_id"],
        claim["lease_id"],
        summary="unsafe completion",
        repo_path=repo,
        queue_path=paths["queue_path"],
        contracts_path=paths["contracts_path"],
        state_path=paths["state_path"],
    )

    queue = json.loads(paths["queue_path"].read_text(encoding="utf-8"))
    assert result["status"] == "needs_review"
    assert queue[0]["status"] == "needs_review"
    assert "SHELL_TRUE" in json.dumps(queue[0]["verification_reasons"])
    assert queue[0]["completion_commit"] == _git(repo, "rev-parse", "HEAD")


def test_finish_allows_clean_committed_python(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Coordinator Test")
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "base.txt")
    _git(repo, "commit", "-m", "base")

    task = _task("shared-task")
    task["allowed_files"] = ["artifact.txt", "tool.py"]
    paths = _state_files(tmp_path / "state", [task])
    _assign(paths, "shared-task", "codex", repo_path=repo)
    claim = claim_next("codex", repo_path=repo, **paths)
    (repo / "tool.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "tool.py")
    _git(repo, "commit", "-m", "clean tool")

    result = finish(
        "codex",
        claim["task_id"],
        claim["lease_id"],
        summary="clean completion",
        repo_path=repo,
        queue_path=paths["queue_path"],
        contracts_path=paths["contracts_path"],
        state_path=paths["state_path"],
    )

    assert result["status"] == "awaiting_codex_review"
    queue = json.loads(paths["queue_path"].read_text(encoding="utf-8"))
    assert queue[0]["status"] == "awaiting_codex_review"
    assert queue[0]["completion_evidence"]["commit_gate"]["passed"] is True


def test_finish_rejects_expired_lease(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Coordinator Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    paths = _state_files(tmp_path / "state", [_task("shared-task")])
    started = dt.datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    _assign(paths, "shared-task", "codex", repo_path=repo, now=started)
    claim = claim_next(
        "codex",
        repo_path=repo,
        now=started,
        lease_seconds=10,
        **paths,
    )

    with pytest.raises(CoordinationError, match="expired"):
        finish(
            "codex",
            claim["task_id"],
            claim["lease_id"],
            summary="too late",
            repo_path=repo,
            queue_path=paths["queue_path"],
            contracts_path=paths["contracts_path"],
            state_path=paths["state_path"],
            now=started + dt.timedelta(seconds=11),
        )


def test_finish_rechecks_lease_expiry_after_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Coordinator Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    paths = _state_files(tmp_path / "state", [_task("shared-task")])
    started = dt.datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    _assign(paths, "shared-task", "codex", repo_path=repo, now=started)
    claim = claim_next("codex", repo_path=repo, now=started, **paths)
    (repo / "artifact.txt").write_text("done\n", encoding="utf-8")
    _git(repo, "add", "artifact.txt")
    _git(repo, "commit", "-m", "artifact")
    times = iter([started, started + dt.timedelta(seconds=120)])
    monkeypatch.setattr(agent_coordinator, "_now", lambda: next(times))

    with pytest.raises(CoordinationError, match="expired during"):
        finish(
            "codex",
            claim["task_id"],
            claim["lease_id"],
            summary="verification overran lease",
            repo_path=repo,
            queue_path=paths["queue_path"],
            contracts_path=paths["contracts_path"],
            state_path=paths["state_path"],
        )

    queue = json.loads(paths["queue_path"].read_text(encoding="utf-8"))
    assert queue[0]["status"] == "in_progress"


def test_finish_state_write_failure_does_not_persist_done(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Coordinator Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    paths = _state_files(tmp_path / "state", [_task("shared-task")])
    _assign(paths, "shared-task", "codex", repo_path=repo)
    claim = claim_next("codex", repo_path=repo, **paths)
    (repo / "artifact.txt").write_text("done\n", encoding="utf-8")
    _git(repo, "add", "artifact.txt")
    _git(repo, "commit", "-m", "artifact")
    original_write = agent_coordinator._atomic_write_json

    def fail_state(path: Path, payload: object) -> None:
        if Path(path) == paths["state_path"]:
            raise OSError("state unavailable")
        original_write(path, payload)

    monkeypatch.setattr(agent_coordinator, "_atomic_write_json", fail_state)

    with pytest.raises(OSError, match="state unavailable"):
        finish(
            "codex",
            claim["task_id"],
            claim["lease_id"],
            summary="artifact complete",
            repo_path=repo,
            queue_path=paths["queue_path"],
            contracts_path=paths["contracts_path"],
            state_path=paths["state_path"],
        )

    queue = json.loads(paths["queue_path"].read_text(encoding="utf-8"))
    assert queue[0]["status"] == "in_progress"


def test_finish_queue_commit_failure_does_not_persist_done(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Coordinator Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    paths = _state_files(tmp_path / "state", [_task("shared-task")])
    _assign(paths, "shared-task", "codex", repo_path=repo)
    claim = claim_next("codex", repo_path=repo, **paths)
    (repo / "artifact.txt").write_text("done\n", encoding="utf-8")
    _git(repo, "add", "artifact.txt")
    _git(repo, "commit", "-m", "artifact")
    original_write = agent_coordinator._atomic_write_json
    queue_writes = 0

    def fail_queue_commit(path: Path, payload: object) -> None:
        nonlocal queue_writes
        if Path(path) == paths["queue_path"]:
            queue_writes += 1
            if queue_writes == 2:
                raise OSError("queue unavailable")
        original_write(path, payload)

    monkeypatch.setattr(agent_coordinator, "_atomic_write_json", fail_queue_commit)

    with pytest.raises(OSError, match="queue unavailable"):
        finish(
            "codex",
            claim["task_id"],
            claim["lease_id"],
            summary="artifact complete",
            repo_path=repo,
            queue_path=paths["queue_path"],
            contracts_path=paths["contracts_path"],
            state_path=paths["state_path"],
        )

    queue = json.loads(paths["queue_path"].read_text(encoding="utf-8"))
    assert queue[0]["status"] == "in_progress"
    state = json.loads(paths["state_path"].read_text(encoding="utf-8"))
    pending = state["agents"]["codex"]["pending_completion"]
    assert pending["task_id"] == claim["task_id"]
    assert pending["completion_commit"] == _git(repo, "rev-parse", "HEAD")


def test_finish_refuses_uncommitted_shared_checkout_changes(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Coordinator Test")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    paths = _state_files(tmp_path / "state", [_task("shared-task")])
    _assign(paths, "shared-task", "codex", repo_path=repo)
    claim = claim_next("codex", repo_path=repo, **paths)
    (repo / "artifact.txt").write_text("dirty\n")

    with pytest.raises(CoordinationError, match="dirty"):
        finish(
            "codex",
            claim["task_id"],
            claim["lease_id"],
            summary="not committed",
            repo_path=repo,
            queue_path=paths["queue_path"],
            contracts_path=paths["contracts_path"],
            state_path=paths["state_path"],
        )


def test_contract_outputs_and_entry_point_strengthen_legacy_queue_spec(
    tmp_path: Path,
):
    task = _task("legacy-task")
    task["allowed_files"] = []
    task["verification_commands"] = []
    paths = _state_files(tmp_path, [task])
    _assign(paths, "legacy-task", "claude")

    result = _claim(paths, "claude")

    assert result["status"] == "claimed"
    queue = json.loads(paths["queue_path"].read_text())
    assert queue[0]["lease_contract_sha256"]
    assert queue[0]["lease_task_spec_sha256"]


def test_finish_rejects_contract_changed_after_claim(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Coordinator Test")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    paths = _state_files(tmp_path / "state", [_task("shared-task")])
    _assign(paths, "shared-task", "codex", repo_path=repo)
    claim = claim_next("codex", repo_path=repo, **paths)
    (repo / "artifact.txt").write_text("done\n")
    _git(repo, "add", "artifact.txt")
    _git(repo, "commit", "-m", "add artifact")
    contracts = json.loads(paths["contracts_path"].read_text())
    contracts[0]["postconditions"].append("new condition")
    paths["contracts_path"].write_text(json.dumps(contracts), encoding="utf-8")

    with pytest.raises(CoordinationError, match="changed after"):
        finish(
            "codex",
            claim["task_id"],
            claim["lease_id"],
            summary="contract changed",
            repo_path=repo,
            queue_path=paths["queue_path"],
            contracts_path=paths["contracts_path"],
            state_path=paths["state_path"],
        )


def test_active_row_without_valid_lease_is_quarantined(tmp_path: Path):
    task = _task("legacy-deadlock", status="in_progress")
    task["assigned_to"] = "orchestrator_loop.py"
    paths = _state_files(tmp_path, [task])

    result = _claim(paths, "codex")

    assert result["status"] == "idle"
    assert result["expired_leases"] == ["legacy-deadlock"]
    queue = json.loads(paths["queue_path"].read_text(encoding="utf-8"))
    assert queue[0]["status"] == "unverified"
    assert queue[0]["orchestration_state"] == "legacy_quarantined"
    assert queue[0]["verification_failure_class"] == "invalid_active_lease"


def test_forged_future_lease_is_quarantined(tmp_path: Path):
    task = _task("forged-lease", status="in_progress")
    task.update(
        {
            "assigned_to": "claude",
            "lease_owner": "claude",
            "lease_id": "lease_forged",
            "lease_expires_at": "2099-01-01T00:00:00+00:00",
        }
    )
    paths = _state_files(tmp_path, [task])

    result = _claim(paths, "codex")

    assert result["expired_leases"] == ["forged-lease"]
    queue = json.loads(paths["queue_path"].read_text(encoding="utf-8"))
    assert queue[0]["status"] == "unverified"


def test_heartbeat_cannot_resurrect_expired_lease(tmp_path: Path):
    paths = _state_files(tmp_path, [_task("expired-heartbeat")])
    started = dt.datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _assign(paths, "expired-heartbeat", "claude", now=started)
    claim = _claim(paths, "claude", now=started, lease_seconds=10)

    with pytest.raises(CoordinationError, match="expired before heartbeat"):
        heartbeat(
            "claude",
            claim["task_id"],
            claim["lease_id"],
            now=started + dt.timedelta(seconds=11),
            queue_path=paths["queue_path"],
            state_path=paths["state_path"],
        )


def test_only_one_codex_assignment_can_be_open(tmp_path: Path):
    paths = _state_files(tmp_path, [_task("first"), _task("second")])
    _assign(paths, "first", "claude")

    with pytest.raises(CoordinationError, match="another Codex assignment is open"):
        _assign(paths, "second", "codex")


def test_codex_assignment_promotes_unassigned_proposal(tmp_path: Path):
    paths = _state_files(tmp_path, [_task("proposal")])
    queue = json.loads(paths["queue_path"].read_text(encoding="utf-8"))
    queue[0]["assigned_ai"] = None
    paths["queue_path"].write_text(json.dumps(queue), encoding="utf-8")

    result = _assign(paths, "proposal", "claude")

    assert result["status"] == "assigned"
    queue = json.loads(paths["queue_path"].read_text(encoding="utf-8"))
    assert queue[0]["assigned_ai"] == "claude"
    assert queue[0]["worker_type"] == "claude"
    assert queue[0]["orchestration_state"] == "assigned"


def test_claim_invalidates_assignment_when_base_commit_changes(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Coordinator Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    paths = _state_files(tmp_path / "state", [_task("base-bound")])
    _assign(paths, "base-bound", "claude", repo_path=repo)
    (repo / "other.txt").write_text("new head\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-m", "move head")

    result = claim_next("claude", repo_path=repo, **paths)

    assert result["status"] == "idle"
    queue = json.loads(paths["queue_path"].read_text(encoding="utf-8"))
    assert queue[0]["status"] == "blocked"
    assert queue[0]["orchestration_state"] == "invalidated"


def test_assignment_rechecks_head_before_persisting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    paths = _state_files(tmp_path / "state", [_task("moving-head")])
    heads = iter(["a" * 40, "b" * 40])
    monkeypatch.setattr(agent_coordinator, "_clean_repo_head", lambda _repo: next(heads))

    with pytest.raises(CoordinationError, match="HEAD changed"):
        _assign(paths, "moving-head", "claude", repo_path=tmp_path)

    queue = json.loads(paths["queue_path"].read_text(encoding="utf-8"))
    assert queue[0]["status"] == "proposed"
    assert "orchestration_id" not in queue[0]


def test_poc_is_approved_only_after_codex_acceptance(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Coordinator Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    paths = _state_files(tmp_path / "state", [_task("poc-task")])
    _assign(paths, "poc-task", "claude", stage="poc", repo_path=repo)
    claim = claim_next("claude", repo_path=repo, **paths)
    (repo / "artifact.txt").write_text("proof\n", encoding="utf-8")
    _git(repo, "add", "artifact.txt")
    _git(repo, "commit", "-m", "prove concept")

    result = finish(
        "claude",
        claim["task_id"],
        claim["lease_id"],
        summary="POC verified",
        repo_path=repo,
        queue_path=paths["queue_path"],
        contracts_path=paths["contracts_path"],
        state_path=paths["state_path"],
    )
    queue = json.loads(paths["queue_path"].read_text(encoding="utf-8"))
    assert result["status"] == "awaiting_codex_review"
    assert "poc_approval_sha256" not in queue[0]

    review_completion(
        "poc-task",
        decision="accept",
        summary="POC evidence supports implementation",
        repo_path=repo,
        queue_path=paths["queue_path"],
        contracts_path=paths["contracts_path"],
        state_path=paths["state_path"],
    )
    queue = json.loads(paths["queue_path"].read_text(encoding="utf-8"))
    assert queue[0]["poc_approved_by"] == "codex"
    assert len(queue[0]["poc_approval_sha256"]) == 64


def test_parent_poc_digest_is_recomputed_before_child_claim(tmp_path: Path):
    parent = _task("poc-parent", status="done")
    parent.update(
        {
            "orchestrated_by": "codex",
            "orchestration_stage": "poc",
            "orchestration_state": "completed",
            "completion_commit": "b" * 40,
            "poc_approved_by": "codex",
        }
    )
    parent_contract = _contract(parent)
    parent["poc_approval_sha256"] = agent_coordinator._poc_approval_digest(
        "poc-parent",
        task_contract_digest(parent_contract),
        normalized_task_spec_digest(parent),
        parent["completion_commit"],
    )
    child = _task("poc-child")
    child["constraints"] = {"local_first": True, "poc_required": True}
    paths = _state_files(tmp_path, [parent, child])
    _assign(
        paths,
        "poc-child",
        "claude",
        parent_task_id="poc-parent",
    )
    queue = json.loads(paths["queue_path"].read_text(encoding="utf-8"))
    queue[0]["completion_commit"] = "c" * 40
    paths["queue_path"].write_text(json.dumps(queue), encoding="utf-8")

    result = _claim(paths, "claude")

    assert result["status"] == "idle"
    queue = json.loads(paths["queue_path"].read_text(encoding="utf-8"))
    child_row = next(item for item in queue if item["id"] == "poc-child")
    assert child_row["status"] == "blocked"
    assert child_row["orchestration_state"] == "invalidated"
