from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest

from harness.agent_coordinator import (
    CoordinationError,
    claim_next,
    finish,
    heartbeat,
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
    assigned_to: str | None = None,
    status: str = "queued",
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
        "assigned_ai": assigned_to or "claude",
        "legacy_adapter": False,
        "priority": priority,
        "status": status,
        "assigned_to": assigned_to,
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


def test_claim_respects_agent_ownership_and_records_lease(tmp_path: Path):
    tasks = [
        _task("claude-task", priority=0, assigned_to="Claude (Cowork)"),
        _task("codex-task", priority=1, assigned_to="Codex"),
        _task("shared-task", priority=2),
    ]
    paths = _state_files(tmp_path, tasks)

    result = _claim(paths, "codex")

    assert result["status"] == "claimed"
    assert result["task_id"] == "codex-task"
    queue = json.loads(paths["queue_path"].read_text())
    claimed = next(task for task in queue if task["contract_id"] == "codex-task")
    assert claimed["status"] == "in_progress"
    assert claimed["lease_owner"] == "codex"
    assert claimed["lease_id"] == result["lease_id"]


def test_cooldown_blocks_agent_claim(tmp_path: Path):
    paths = _state_files(tmp_path, [_task("shared-task")])
    now = dt.datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    set_cooldown("claude", seconds=600, reason="rate limit", now=now, **{
        "queue_path": paths["queue_path"],
        "state_path": paths["state_path"],
    })

    result = _claim(paths, "claude", now=now + dt.timedelta(seconds=1))

    assert result["status"] == "cooldown"
    assert result["cooldown_until"] == "2026-07-16T12:10:00+00:00"


def test_takeover_requires_assigned_agent_to_be_cooling(tmp_path: Path):
    paths = _state_files(
        tmp_path,
        [_task("claude-task", assigned_to="jarvis-general-claude-123")],
    )
    now = dt.datetime(2026, 7, 16, 12, 0, tzinfo=UTC)

    assert _claim(paths, "codex", takeover_cooling=True, now=now)["status"] == "idle"

    set_cooldown("claude", seconds=600, reason="credit exhausted", now=now, **{
        "queue_path": paths["queue_path"],
        "state_path": paths["state_path"],
    })
    result = _claim(
        paths,
        "codex",
        takeover_cooling=True,
        now=now + dt.timedelta(seconds=1),
    )

    assert result["status"] == "claimed"
    assert result["task_id"] == "claude-task"


def test_approval_task_waits_for_exact_digest_bound_approval(tmp_path: Path):
    task = _task("approval-task")
    paths = _state_files(tmp_path, [task], approval_ids={"approval-task"})

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
    first = _claim(paths, "codex")

    second = _claim(paths, "claude")

    assert first["status"] == "claimed"
    assert second["status"] == "capacity"
    assert second["task_ids"] == ["first-task"]


def test_cooldown_releases_owned_lease_for_failover(tmp_path: Path):
    paths = _state_files(tmp_path, [_task("shared-task")])
    now = dt.datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    first = _claim(paths, "claude", now=now)

    cooldown = set_cooldown(
        "claude",
        seconds=600,
        reason="session limit",
        now=now + dt.timedelta(seconds=10),
        queue_path=paths["queue_path"],
        state_path=paths["state_path"],
    )
    second = _claim(
        paths,
        "codex",
        takeover_cooling=True,
        now=now + dt.timedelta(seconds=11),
    )

    assert cooldown["released_tasks"] == ["shared-task"]
    assert second["status"] == "claimed"
    assert second["task_id"] == first["task_id"]


def test_expired_lease_starts_owner_cooldown_and_is_reclaimed(tmp_path: Path):
    paths = _state_files(tmp_path, [_task("shared-task")])
    now = dt.datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    _claim(paths, "claude", now=now, lease_seconds=10)

    result = _claim(
        paths,
        "codex",
        takeover_cooling=True,
        now=now + dt.timedelta(seconds=11),
    )

    assert result["status"] == "claimed"
    assert result["expired_leases"] == ["shared-task"]
    snapshot = status_snapshot(
        queue_path=paths["queue_path"],
        state_path=paths["state_path"],
    )
    assert snapshot["coordination"]["agents"]["claude"]["status"] == "cooldown"


def test_heartbeat_rejects_wrong_agent(tmp_path: Path):
    paths = _state_files(tmp_path, [_task("shared-task")])
    claim = _claim(paths, "claude")

    with pytest.raises(CoordinationError, match="ownership"):
        heartbeat(
            "codex",
            claim["task_id"],
            claim["lease_id"],
            queue_path=paths["queue_path"],
            state_path=paths["state_path"],
        )


def test_finish_uses_loop_evidence_before_marking_done(tmp_path: Path):
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
    claim = claim_next("codex", repo_path=repo, **paths)
    (repo / "artifact.txt").write_text("done\n")
    _git(repo, "add", "artifact.txt")
    _git(repo, "commit", "-m", "add artifact")

    result = finish(
        "codex",
        claim["task_id"],
        claim["lease_id"],
        summary="artifact complete",
        repo_path=repo,
        queue_path=paths["queue_path"],
        contracts_path=paths["contracts_path"],
        state_path=paths["state_path"],
    )

    assert result["status"] == "verified"
    queue = json.loads(paths["queue_path"].read_text())
    assert queue[0]["status"] == "done"
    assert queue[0]["completion_commit"] == _git(repo, "rev-parse", "HEAD")
    assert queue[0]["completion_evidence"]["changed_files"] == ["artifact.txt"]


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
