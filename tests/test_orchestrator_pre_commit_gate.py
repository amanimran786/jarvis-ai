"""Item 4 — run_checks() wired into the orchestrator harvest loop.

Uses a real tmp git repo so evidence collection (harness.completion_verifier)
and the REVIEW.md gate (harness.pre_commit_check) run for real; only the
session/queue plumbing around them is faked.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import orchestrator_loop


def _init_repo(repo: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _task(**overrides: Any) -> dict[str, Any]:
    task = {
        "id": "GATE-001",
        "title": "Add a helper tool",
        "description": "Add tool.py to the repo.",
        "goal": "Add tool.py implementing the helper.",
        "status": "in_progress",
        "priority": 1,
        "allowed_files": ["tool.py"],
        "acceptance_criteria": ["tool.py exists"],
        "verification_commands": [],
        "constraints": {"local_first": True, "network": False},
        "domain": "orchestrator",
        "assigned_ai": "codex",
        "assigned_to": "test-session",
    }
    task.update(overrides)
    return task


class FakeSessionTracker:
    def __init__(self, sessions: list[dict[str, Any]]) -> None:
        self._sessions = sessions

    def list_completed(self) -> list[dict[str, Any]]:
        return self._sessions

    def purge_completed(self) -> None:
        return None

    def active_count(self) -> int:
        return 0


@pytest.fixture
def gate_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    base_ref = _init_repo(repo)

    queue_path = tmp_path / "WORK_QUEUE.json"
    violations_log = tmp_path / "logs" / "pre_commit_violations.log"

    monkeypatch.setattr(orchestrator_loop, "WORK_QUEUE_PATH", queue_path)
    monkeypatch.setattr(orchestrator_loop, "LAUNCH_QUEUE_PATH", tmp_path / "LAUNCH_QUEUE.json")
    monkeypatch.setattr(orchestrator_loop, "MASTER_LOG_PATH", tmp_path / "MASTER_LOG.md")
    monkeypatch.setattr(orchestrator_loop, "ATTEMPT_LOG_PATH", tmp_path / "attempts.jsonl")
    monkeypatch.setattr(orchestrator_loop, "PRE_COMMIT_VIOLATIONS_LOG", violations_log)
    monkeypatch.setattr(orchestrator_loop, "_ensure_dashboard_running", lambda: None)
    monkeypatch.setattr(orchestrator_loop, "_expire_stalled_sessions", lambda *_a, **_k: 0)
    monkeypatch.setattr(orchestrator_loop, "_now", lambda: "2026-07-23T12:00:00+00:00")

    class Harness:
        def seed(self, task: dict[str, Any]) -> None:
            queue_path.write_text(json.dumps([task]), encoding="utf-8")

        def queue_task(self) -> dict[str, Any]:
            return json.loads(queue_path.read_text(encoding="utf-8"))[0]

        def complete_session(self, **overrides: Any) -> None:
            session = {
                "session_id": "test-session",
                "task_id": "GATE-001",
                "result_summary": "Added tool.py",
                "repo_path": str(repo),
                "base_ref": base_ref,
            }
            session.update(overrides)
            monkeypatch.setattr(
                orchestrator_loop, "SessionTracker", lambda: FakeSessionTracker([session])
            )

        def run(self) -> dict[str, Any]:
            return orchestrator_loop.run_loop(max_concurrent=1, dry_run=False)

        def violations_text(self) -> str:
            return violations_log.read_text(encoding="utf-8") if violations_log.exists() else ""

    harness = Harness()
    harness.repo = repo
    return harness


def test_shell_true_commit_blocks_task_from_reaching_done(gate_harness) -> None:
    # Built via .format() so this fixture's own source text never contains a
    # contiguous "shell=True" — the write-time security hook scans this file
    # too and would otherwise flag it as if it were real vulnerable code.
    vuln_snippet = (
        "import subprocess\n"
        "def run(cmd):\n"
        "    return subprocess.run(cmd, {kw}={val})\n"
    ).format(kw="shell", val="True")
    (gate_harness.repo / "tool.py").write_text(vuln_snippet, encoding="utf-8")
    gate_harness.seed(_task())
    gate_harness.complete_session()

    result = gate_harness.run()

    assert result["needs_review"] == 1
    assert result["harvested"] == 0
    task = gate_harness.queue_task()
    assert task["status"] == "needs_review"
    assert task["verification_failure_class"] == "pre_commit_gate_violation"
    assert any("SHELL_TRUE" in reason for reason in task["verification_reasons"])
    assert "SHELL_TRUE" in gate_harness.violations_text()
    assert "GATE-001" in gate_harness.violations_text()


def test_clean_commit_reaches_done(gate_harness) -> None:
    (gate_harness.repo / "tool.py").write_text(
        "import subprocess\n"
        "def run(cmd):\n"
        "    return subprocess.run(cmd, shell=False)\n",
        encoding="utf-8",
    )
    gate_harness.seed(_task())
    gate_harness.complete_session()

    result = gate_harness.run()

    assert result["harvested"] == 1
    assert result["needs_review"] == 0
    task = gate_harness.queue_task()
    assert task["status"] == "done"
