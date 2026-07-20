from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness.completion_verifier import (
    MAX_CAPTURE_BYTES,
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
    (tmp_path / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n")
    (tmp_path / "tracked.txt").write_text("base\n")
    _git(tmp_path, "add", ".gitignore", "tracked.txt")
    _git(tmp_path, "commit", "-m", "base")
    return tmp_path, _git(tmp_path, "rev-parse", "HEAD")


def _spec(*commands: str, wall_time_seconds: int = 30) -> TaskSpec:
    return TaskSpec.from_queue_task(
        {
            "id": "VERIFY-1",
            "title": "Verify a temporary repository",
            "goal": "Collect completion evidence",
            "allowed_files": [
                "tracked.txt",
                "committed.txt",
                "untracked.txt",
                "generated.txt",
            ],
            "verification_commands": list(commands),
            "budget": {"wall_time_seconds": wall_time_seconds},
            "assigned_ai": "codex",
        }
    )


def _install_test(worktree: Path, source: str) -> str:
    (worktree / "verify_test.py").write_text(source)
    _git(worktree, "add", "verify_test.py")
    _git(worktree, "commit", "-m", "add verifier fixture")
    return _git(worktree, "rev-parse", "HEAD")


def _pytest_command(*args: str) -> str:
    argv = [sys.executable, "-m", "pytest", "verify_test.py", "-q", *args]
    return shlex.join(argv)


def test_collects_committed_worktree_and_untracked_changes(repo: tuple[Path, str]):
    worktree, base = repo
    (worktree / "tracked.txt").write_text("changed\n")
    (worktree / "committed.txt").write_text("committed\n")
    _git(worktree, "add", "committed.txt")
    _git(worktree, "commit", "-m", "committed change")
    (worktree / "untracked.txt").write_text("untracked\n")

    evidence = collect_completion_evidence(_spec(), worktree, base)

    expected = ["committed.txt", "tracked.txt", "untracked.txt"]
    assert evidence["observer"] == "loop"
    assert evidence["changed_files"] == expected
    assert evidence["changed_files_before_commands"] == expected
    assert evidence["changed_files_after_commands"] == expected
    assert evidence["commands"] == []
    assert evidence["policy_findings"] == []
    assert evidence["evidence_policy"]["full_sandbox"] is False


def test_command_evidence_is_hard_capped_with_hash_and_preview(
    repo: tuple[Path, str],
):
    worktree, _ = repo
    base = _install_test(
        worktree,
        "import os\n\ndef test_output():\n"
        f"    os.write(1, b'x' * ({MAX_CAPTURE_BYTES} + 4096))\n",
    )
    command = _pytest_command("-s")

    evidence = collect_completion_evidence(_spec(command), worktree, base)
    result = evidence["commands"][0]

    assert result["command"] == command
    assert result["exit_code"] == 125
    assert result["stdout_preview"] == "x" * PREVIEW_BYTES
    assert result["stdout_sha256"] == hashlib.sha256(
        b"x" * MAX_CAPTURE_BYTES
    ).hexdigest()
    assert result["stdout_bytes"] == MAX_CAPTURE_BYTES
    assert result["stdout_truncated"] is True
    assert result["output_overflow"] is True


def test_failed_command_records_output_and_is_rejected(repo: tuple[Path, str]):
    worktree, _ = repo
    base = _install_test(
        worktree,
        "def test_failure():\n    raise AssertionError('nope')\n",
    )
    (worktree / "tracked.txt").write_text("changed\n")
    command = _pytest_command()
    spec = _spec(command)

    evidence = collect_completion_evidence(spec, worktree, base)
    verdict = evaluate_completion(spec, evidence)

    assert evidence["commands"][0]["exit_code"] == 1
    assert "nope" in evidence["commands"][0]["stdout_preview"]
    assert verdict.failure_class == "test_failure"


@pytest.mark.parametrize(
    "command",
    [
        "python -c 'print(1)'",
        "python verify_test.py",
        "/usr/bin/true",
        "python -m os",
        "python -m ruff check --fix .",
        "git reset --hard",
    ],
)
def test_untrusted_commands_fail_without_execution(
    repo: tuple[Path, str], command: str
):
    worktree, base = repo

    evidence = collect_completion_evidence(_spec(command), worktree, base)
    result = evidence["commands"][0]

    assert result["exit_code"] == 126
    assert result["policy_validated"] is False
    assert result["stderr_preview"]


def test_shell_syntax_rejects_entire_command_without_side_effect(
    repo: tuple[Path, str],
):
    worktree, _ = repo
    base = _install_test(worktree, "def test_ok():\n    assert True\n")
    marker = worktree / "should-not-exist"
    command = f"{_pytest_command()} ; /usr/bin/touch {shlex.quote(str(marker))}"

    evidence = collect_completion_evidence(_spec(command), worktree, base)

    assert evidence["commands"][0]["exit_code"] == 126
    assert marker.exists() is False


def test_read_only_git_checks_are_allowed(repo: tuple[Path, str]):
    worktree, base = repo
    (worktree / "tracked.txt").write_text("changed\n")
    commands = ("git status --short", "git diff --check -- tracked.txt")

    evidence = collect_completion_evidence(_spec(*commands), worktree, base)

    assert [result["exit_code"] for result in evidence["commands"]] == [0, 0]
    assert all(result["policy_validated"] for result in evidence["commands"])


def test_successful_evidence_is_accepted_by_completion_gate(repo: tuple[Path, str]):
    worktree, _ = repo
    base = _install_test(worktree, "def test_ok():\n    assert True\n")
    (worktree / "tracked.txt").write_text("done\n")
    command = _pytest_command()
    spec = _spec(command)

    evidence = collect_completion_evidence(spec, worktree, base)

    assert evaluate_completion(spec, evidence).passed is True


def test_rejects_replace_refs(repo: tuple[Path, str]):
    worktree, base = repo
    _git(worktree, "update-ref", f"refs/replace/{base}", base)

    with pytest.raises(CompletionEvidenceError, match="replacement refs"):
        collect_completion_evidence(_spec(), worktree, base)


@pytest.mark.parametrize("flag", ["--skip-worktree", "--assume-unchanged"])
def test_rejects_hidden_index_flags(repo: tuple[Path, str], flag: str):
    worktree, base = repo
    _git(worktree, "update-index", flag, "tracked.txt")

    with pytest.raises(CompletionEvidenceError, match="skip-worktree or assume-unchanged"):
        collect_completion_evidence(_spec(), worktree, base)


def test_timeout_kills_descendant_process_group(repo: tuple[Path, str]):
    worktree, _ = repo
    marker = worktree / "descendant-leaked"
    source = f"""
import subprocess
import sys
import time

def test_spawn_descendant():
    subprocess.Popen([
        sys.executable,
        "-c",
        "import time; from pathlib import Path; time.sleep(1.5); "
        "Path({str(marker)!r}).write_text('leaked')",
    ])
    time.sleep(10)
"""
    base = _install_test(worktree, source)
    command = _pytest_command("-s")

    evidence = collect_completion_evidence(
        _spec(command, wall_time_seconds=1), worktree, base
    )
    time.sleep(2)

    assert evidence["commands"][0]["exit_code"] == 124
    assert evidence["commands"][0]["timed_out"] is True
    assert marker.exists() is False


def test_worktree_mutation_emits_open_policy_finding(repo: tuple[Path, str]):
    worktree, _ = repo
    base = _install_test(
        worktree,
        "from pathlib import Path\n\n"
        "def test_mutate():\n    Path('generated.txt').write_text('created')\n",
    )
    command = _pytest_command()
    spec = _spec(command)

    evidence = collect_completion_evidence(spec, worktree, base)
    finding = evidence["policy_findings"][0]

    assert evidence["changed_files_before_commands"] == []
    assert evidence["changed_files_after_commands"] == ["generated.txt"]
    assert finding["status"] == "open"
    assert finding["added"] == ["generated.txt"]
    assert evaluate_completion(spec, evidence).failure_class == "policy_failure"


def test_mutation_of_already_changed_file_is_detected(repo: tuple[Path, str]):
    worktree, _ = repo
    base = _install_test(
        worktree,
        "from pathlib import Path\n\n"
        "def test_mutate():\n    Path('tracked.txt').write_text('after verifier')\n",
    )
    (worktree / "tracked.txt").write_text("before verifier\n")
    command = _pytest_command()

    evidence = collect_completion_evidence(_spec(command), worktree, base)

    assert evidence["changed_files_before_commands"] == ["tracked.txt"]
    assert evidence["changed_files_after_commands"] == ["tracked.txt"]
    assert evidence["policy_findings"][0]["modified"] == ["tracked.txt"]


def test_rejects_unknown_base_ref(repo: tuple[Path, str]):
    worktree, _ = repo

    with pytest.raises(CompletionEvidenceError):
        collect_completion_evidence(_spec(), worktree, "--not-a-ref")


def test_rejects_active_repository_local_excludes(repo: tuple[Path, str]):
    worktree, base = repo
    exclude = worktree / _git(worktree, "rev-parse", "--git-path", "info/exclude")
    exclude.write_text("hidden.txt\n", encoding="utf-8")

    with pytest.raises(CompletionEvidenceError, match="info/exclude"):
        collect_completion_evidence(_spec(), worktree, base)


def test_verification_environment_scrubs_secret_variables(
    repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
):
    worktree, _ = repo
    base = _install_test(
        worktree,
        "import os\n\ndef test_secret_absent():\n"
        "    assert 'TEST_SECRET_TOKEN' not in os.environ\n",
    )
    monkeypatch.setenv("TEST_SECRET_TOKEN", "must-not-leak")

    evidence = collect_completion_evidence(_spec(_pytest_command()), worktree, base)

    assert evidence["commands"][0]["exit_code"] == 0
