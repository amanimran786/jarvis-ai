from __future__ import annotations

import hashlib
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from harness.completion_verifier import (
    PREVIEW_BYTES,
    CompletionEvidenceError,
    collect_completion_evidence,
)
from harness.task_contract import TaskSpec, evaluate_completion


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Completion Test")
    (tmp_path / "tracked.txt").write_text("base\n")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "base")
    return tmp_path, _git(tmp_path, "rev-parse", "HEAD")


def _spec(repo: Path, *commands: str) -> TaskSpec:
    return TaskSpec.from_queue_task(
        {
            "id": "VERIFY-1",
            "title": "Verify a temporary repository",
            "goal": "Collect completion evidence",
            "allowed_files": ["tracked.txt", "committed.txt", "untracked.txt"],
            "verification_commands": list(commands),
            "budget": {"wall_time_seconds": 30},
            "assigned_ai": "codex",
        }
    )


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def test_collects_committed_worktree_and_untracked_changes(repo: tuple[Path, str]):
    worktree, base = repo
    (worktree / "tracked.txt").write_text("changed\n")
    (worktree / "committed.txt").write_text("committed\n")
    _git(worktree, "add", "committed.txt")
    _git(worktree, "commit", "-m", "committed change")
    (worktree / "untracked.txt").write_text("untracked\n")

    evidence = collect_completion_evidence(_spec(worktree), worktree, base)

    assert evidence == {
        "observer": "loop",
        "changed_files": ["committed.txt", "tracked.txt", "untracked.txt"],
        "commands": [],
        "policy_findings": [],
    }


def test_command_evidence_has_bounded_preview_and_full_output_hash(
    repo: tuple[Path, str],
):
    worktree, base = repo
    output = "x" * (PREVIEW_BYTES + 100)
    command = _python_command(f"print({output!r}, end='')")

    evidence = collect_completion_evidence(_spec(worktree, command), worktree, base)
    result = evidence["commands"][0]

    assert result["command"] == command
    assert result["exit_code"] == 0
    assert result["stdout_preview"] == output[:PREVIEW_BYTES]
    assert result["stdout_sha256"] == hashlib.sha256(output.encode()).hexdigest()
    assert result["stdout_bytes"] == len(output)
    assert result["stdout_truncated"] is True
    assert result["stderr_sha256"] == hashlib.sha256(b"").hexdigest()


def test_failed_command_records_stderr_and_is_rejected(repo: tuple[Path, str]):
    worktree, base = repo
    (worktree / "tracked.txt").write_text("changed\n")
    command = _python_command("import sys; print('nope', file=sys.stderr); sys.exit(7)")
    spec = _spec(worktree, command)

    evidence = collect_completion_evidence(spec, worktree, base)
    verdict = evaluate_completion(spec, evidence)

    assert evidence["commands"][0]["exit_code"] == 7
    assert evidence["commands"][0]["stderr_preview"] == "nope\n"
    assert verdict.failure_class == "test_failure"


def test_shell_metacharacters_are_not_executed(repo: tuple[Path, str]):
    worktree, base = repo
    marker = worktree / "should-not-exist"
    command = (
        f"{_python_command('print(\"verified\")')} ; "
        f"{shlex.quote(sys.executable)} -c "
        f"{shlex.quote(f'from pathlib import Path; Path({str(marker)!r}).touch()')}"
    )

    evidence = collect_completion_evidence(_spec(worktree, command), worktree, base)

    assert evidence["commands"][0]["exit_code"] == 0
    assert evidence["commands"][0]["stdout_preview"] == "verified\n"
    assert marker.exists() is False


def test_successful_evidence_is_accepted_by_completion_gate(repo: tuple[Path, str]):
    worktree, base = repo
    (worktree / "tracked.txt").write_text("done\n")
    command = _python_command("print('ok')")
    spec = _spec(worktree, command)

    evidence = collect_completion_evidence(spec, worktree, base)

    assert evaluate_completion(spec, evidence).passed is True


def test_rejects_unknown_base_ref(repo: tuple[Path, str]):
    worktree, _ = repo

    with pytest.raises(CompletionEvidenceError):
        collect_completion_evidence(_spec(worktree), worktree, "--not-a-ref")
