from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.runtime_launcher import process_runtime_queue
from harness.session_tracker import SessionTracker
from harness.task_contract import TaskSpec


class FakeRuntime:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.cancelled: list[str] = []

    def submit_task(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        task = {
            "id": "runtime-1",
            "status": "queued",
            "workspace": {"enabled": True, "ok": True, "worktree_path": "/tmp/worktree"},
            "result": "",
            "error": "",
            "cancel_requested": False,
        }
        self.tasks["runtime-1"] = task
        return dict(task)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        task = self.tasks.get(task_id)
        return dict(task) if task else None

    def cancel_task(self, task_id: str) -> dict[str, Any] | None:
        self.cancelled.append(task_id)
        return self.get_task(task_id)


def _entry() -> dict[str, Any]:
    spec = TaskSpec.from_queue_task({
        "id": "TASK-1",
        "title": "Implement one change",
        "goal": "Implement one change",
        "allowed_files": ["x.py"],
        "verification_commands": ["python -m compileall -q x.py"],
        "assigned_ai": "local",
    })
    return {
        "task_id": spec.task_id,
        "session_id": "placeholder",
        "attempt_id": "attempt-1",
        "attempt_number": 1,
        "contract_sha256": spec.contract_hash,
        "task_spec": spec.to_dict(),
        "prompt": "rendered prompt",
        "base_ref": "a" * 40,
        "status": "handoff_ready",
    }


def _run(tmp_path: Path, runtime: FakeRuntime, entry: dict[str, Any]):
    queue = tmp_path / "LAUNCH_QUEUE.json"
    queue.write_text(json.dumps([entry]), encoding="utf-8")
    tracker = SessionTracker(path=tmp_path / "ACTIVE_SESSIONS.json")
    changed = process_runtime_queue(queue, task_runtime_module=runtime, tracker=tracker)
    return changed, json.loads(queue.read_text())[0], tracker


def test_handoff_submits_isolated_runtime_and_claims_real_id(tmp_path: Path):
    runtime = FakeRuntime()
    changed, entry, tracker = _run(tmp_path, runtime, _entry())

    assert changed
    assert entry["runtime_task_id"] == "runtime-1"
    assert entry["status"] == "queued"
    assert tracker.list_active()[0]["session_id"] == "runtime-1"
    assert tracker.list_active()[0]["repo_path"] == "/tmp/worktree"


def test_runtime_success_becomes_completed_session(tmp_path: Path):
    runtime = FakeRuntime()
    _, entry, tracker = _run(tmp_path, runtime, _entry())
    runtime.tasks["runtime-1"].update(status="succeeded", result="done")

    _, entry, tracker = _run(tmp_path, runtime, entry)

    assert entry["status"] == "completion_claimed"
    assert tracker.list_completed()[0]["result_summary"] == "done"


def test_runtime_failure_is_classified_for_retry(tmp_path: Path):
    runtime = FakeRuntime()
    _, entry, tracker = _run(tmp_path, runtime, _entry())
    runtime.tasks["runtime-1"].update(status="failed", error="model failed")

    _, entry, tracker = _run(tmp_path, runtime, entry)

    assert entry["status"] == "failed"
    assert tracker.list_completed()[0]["runtime_failure_class"] == "agent_error"


def test_waiting_approval_is_not_auto_approved(tmp_path: Path):
    runtime = FakeRuntime()
    _, entry, _ = _run(tmp_path, runtime, _entry())
    runtime.tasks["runtime-1"].update(
        status="waiting_approval", approval_reason="operator required"
    )

    _, entry, _ = _run(tmp_path, runtime, entry)

    assert entry["status"] == "waiting_approval"
    assert entry["approval_reason"] == "operator required"


def test_legacy_fired_placeholder_is_retired(tmp_path: Path):
    runtime = FakeRuntime()
    entry = _entry()
    entry.update(status="fired")
    tracker = SessionTracker(path=tmp_path / "ACTIVE_SESSIONS.json")
    tracker.claim(entry["task_id"], entry["session_id"])
    queue = tmp_path / "LAUNCH_QUEUE.json"
    queue.write_text(json.dumps([entry]), encoding="utf-8")

    process_runtime_queue(queue, task_runtime_module=runtime, tracker=tracker)

    assert json.loads(queue.read_text())[0]["status"] == "legacy_stale"
    assert tracker.active_count() == 0


def test_legacy_contract_is_not_submitted(tmp_path: Path):
    runtime = FakeRuntime()
    entry = _entry()
    legacy = TaskSpec.from_queue_task({"task": "do something broad"})
    entry.update(task_id=legacy.task_id, task_spec=legacy.to_dict(), contract_sha256=legacy.contract_hash)

    _, entry, tracker = _run(tmp_path, runtime, entry)

    assert entry["status"] == "runtime_error"
    assert not runtime.tasks
    assert tracker.active_count() == 0


def test_inert_queue_does_not_import_or_require_task_runtime(tmp_path: Path):
    queue = tmp_path / "LAUNCH_QUEUE.json"
    queue.write_text(json.dumps([{"status": "legacy_stale"}]), encoding="utf-8")

    assert process_runtime_queue(queue) == []
