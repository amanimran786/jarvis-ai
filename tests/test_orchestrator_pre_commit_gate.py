"""Item 4 — run_checks() wired into the orchestrator harvest loop.

Uses a real tmp git repo so evidence collection (harness.completion_verifier)
and the REVIEW.md gate (harness.pre_commit_check) run for real; only the
session/queue plumbing around them is faked.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from harness import commit_review_gate
import orchestrator_loop


def _init_repo(repo: Path) -> str:
    subprocess.run(
        ["git", "init", "-q"],
        cwd=repo,
        check=True,
        timeout=30,
        shell=False,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo,
        check=True,
        timeout=30,
        shell=False,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        check=True,
        timeout=30,
        shell=False,
    )
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."],
        cwd=repo,
        check=True,
        timeout=30,
        shell=False,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=repo,
        check=True,
        timeout=30,
        shell=False,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
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
        self.purged = False

    def list_completed(self) -> list[dict[str, Any]]:
        return self._sessions

    def purge_completed(self, _session_ids=None) -> None:
        self.purged = True

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

        def commit_tool(self, source: str) -> None:
            (repo / "tool.py").write_text(source, encoding="utf-8")
            subprocess.run(
                ["git", "add", "tool.py"],
                cwd=repo,
                check=True,
                timeout=30,
                shell=False,
            )
            subprocess.run(
                ["git", "commit", "-q", "-m", "complete tool"],
                cwd=repo,
                check=True,
                timeout=30,
                shell=False,
            )

        def complete_session(self, **overrides: Any) -> None:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
                shell=False,
            ).stdout.strip()
            session = {
                "session_id": "test-session",
                "task_id": "GATE-001",
                "result_summary": "Added tool.py",
                "repo_path": str(repo),
                "base_ref": base_ref,
                "completion_commit": head,
            }
            session.update(overrides)
            self.tracker = FakeSessionTracker([session])
            monkeypatch.setattr(orchestrator_loop, "SessionTracker", lambda: self.tracker)

        def run(self) -> dict[str, Any]:
            return orchestrator_loop.run_loop(max_concurrent=1, dry_run=False)

        def violations_text(self) -> str:
            return violations_log.read_text(encoding="utf-8") if violations_log.exists() else ""

    harness = Harness()
    harness.repo = repo
    harness.violations_log = violations_log
    return harness


def test_shell_true_commit_blocks_task_from_reaching_done(gate_harness) -> None:
    # Build the unsafe call at runtime so the test source itself stays gate-clean.
    vuln_snippet = (
        "import subprocess\n"
        "def run(cmd):\n"
        "    return subprocess.run(cmd, {kw}={val})\n"
    ).format(kw="shell", val="True")
    gate_harness.commit_tool(vuln_snippet)
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
    gate_harness.commit_tool(
        "import subprocess\n"
        "def run(cmd):\n"
        "    return subprocess.run(cmd, shell=False)\n",
    )
    gate_harness.seed(_task())
    gate_harness.complete_session()

    result = gate_harness.run()

    assert result["harvested"] == 1
    assert result["needs_review"] == 0
    task = gate_harness.queue_task()
    assert task["status"] == "done"


def test_hardcoded_secret_value_is_not_persisted(gate_harness) -> None:
    secret_value = "credential-" + "must-not-leak"
    secret_name = "API" + "_KEY"
    gate_harness.commit_tool(f"{secret_name} = {secret_value!r}\n")
    gate_harness.seed(_task())
    gate_harness.complete_session()

    result = gate_harness.run()

    assert result["needs_review"] == 1
    task_text = json.dumps(gate_harness.queue_task())
    log_text = gate_harness.violations_text()
    assert "HARDCODED_SECRET" in task_text
    assert "HARDCODED_SECRET" in log_text
    assert secret_value not in task_text
    assert secret_value not in log_text
    assert gate_harness.violations_log.stat().st_mode & 0o777 == 0o600


def test_python_symlink_outside_repo_fails_closed(gate_harness) -> None:
    outside = gate_harness.repo.parent / "outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    (gate_harness.repo / "tool.py").symlink_to(outside)
    subprocess.run(
        ["git", "add", "tool.py"],
        cwd=gate_harness.repo,
        check=True,
        timeout=30,
        shell=False,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "add Python symlink"],
        cwd=gate_harness.repo,
        check=True,
        timeout=30,
        shell=False,
    )
    gate_harness.seed(_task())
    gate_harness.complete_session()

    result = gate_harness.run()

    assert result["needs_review"] == 1
    assert "UNSAFE_GIT_MODE" in json.dumps(gate_harness.queue_task())


def test_gate_exception_fails_closed(
    gate_harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate_harness.commit_tool("VALUE = 1\n")
    monkeypatch.setattr(
        commit_review_gate,
        "run_checks",
        lambda _files: (_ for _ in ()).throw(RuntimeError("gate unavailable")),
    )
    gate_harness.seed(_task())
    gate_harness.complete_session()

    result = gate_harness.run()

    assert result["needs_review"] == 0
    assert result["harvested"] == 0
    assert gate_harness.queue_task()["status"] != "done"


def test_queue_save_failure_does_not_purge_completion(
    gate_harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsafe = "run(command, shell" + "=True)\n"
    gate_harness.commit_tool(unsafe)
    gate_harness.seed(_task())
    gate_harness.complete_session()
    monkeypatch.setattr(
        orchestrator_loop,
        "_save_queue",
        lambda _queue: (_ for _ in ()).throw(OSError("queue unavailable")),
    )

    with pytest.raises(OSError, match="queue unavailable"):
        gate_harness.run()

    assert gate_harness.tracker.purged is False


def test_violation_log_failure_still_persists_needs_review(
    gate_harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsafe = "run(command, shell" + "=True)\n"
    gate_harness.commit_tool(unsafe)
    gate_harness.seed(_task())
    gate_harness.complete_session()
    monkeypatch.setattr(
        orchestrator_loop,
        "write_gate_log",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("log unavailable")),
    )

    result = gate_harness.run()

    assert result["needs_review"] == 1
    assert gate_harness.queue_task()["status"] == "needs_review"


def test_completion_cannot_overwrite_reassigned_task(gate_harness) -> None:
    gate_harness.commit_tool("VALUE = 1\n")
    gate_harness.seed(_task(assigned_to="different-session"))
    gate_harness.complete_session()

    result = gate_harness.run()

    assert result["rejected"] == 1
    assert gate_harness.queue_task()["status"] == "in_progress"
    assert gate_harness.tracker.purged is True


def test_retry_cannot_overwrite_task_reassigned_during_verification(
    gate_harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate_harness.commit_tool("VALUE = 1\n")
    failure_command = shlex.join(
        [sys.executable, "-c", "raise SystemExit(1)"]
    )
    gate_harness.seed(_task(verification_commands=[failure_command]))
    gate_harness.complete_session()
    original_verify = orchestrator_loop.verify_completion

    def verify_then_reassign(*args: Any, **kwargs: Any):
        assessment = original_verify(*args, **kwargs)
        task = gate_harness.queue_task()
        task["assigned_to"] = "different-session"
        gate_harness.seed(task)
        return assessment

    monkeypatch.setattr(
        orchestrator_loop,
        "verify_completion",
        verify_then_reassign,
    )

    with pytest.raises(RuntimeError, match="assignment changed before retry"):
        gate_harness.run()

    assert gate_harness.queue_task()["status"] == "in_progress"
    assert gate_harness.queue_task()["assigned_to"] == "different-session"
    assert gate_harness.tracker.purged is False
