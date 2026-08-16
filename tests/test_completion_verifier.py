from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness import completion_verifier
from harness.completion_verifier import (
    MAX_CAPTURE_BYTES,
    PREVIEW_BYTES,
    CompletionEvidenceError,
    compact_completion_evidence,
    collect_completion_evidence,
    verify_completion,
)
from harness.task_contract import TaskSpec, evaluate_completion


@pytest.fixture(autouse=True)
def _portable_verification_sandbox(monkeypatch: pytest.MonkeyPatch):
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        monkeypatch.setattr(
            completion_verifier,
            "_sandboxed_argv",
            lambda argv, _repo, _root: list(argv),
        )


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
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
                "tool.py",
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
    assert evidence["base_commit"] == base
    assert evidence["completion_commit_before_commands"] == _git(
        worktree, "rev-parse", "HEAD"
    )
    assert evidence["completion_commit"] == _git(worktree, "rev-parse", "HEAD")
    assert evidence["changed_files"] == expected
    assert evidence["changed_files_before_commands"] == expected
    assert evidence["changed_files_after_commands"] == expected
    assert evidence["commands"] == []
    assert evidence["policy_findings"] == []
    assert evidence["evidence_policy"]["full_sandbox"] is (
        sys.platform == "darwin"
        and Path("/usr/bin/sandbox-exec").is_file()
    )


def test_verify_completion_enforces_immutable_commit_gate(
    repo: tuple[Path, str],
) -> None:
    worktree, base = repo
    unsafe = "run(command, shell" + "=True)\n"
    (worktree / "tool.py").write_text(unsafe, encoding="utf-8")
    _git(worktree, "add", "tool.py")
    _git(worktree, "commit", "-m", "unsafe completion")
    completion = _git(worktree, "rev-parse", "HEAD")

    assessment = verify_completion(
        _spec(),
        worktree,
        base,
        completion_ref=completion,
    )

    assert assessment.gate.passed is False
    assert assessment.verdict.passed is False
    assert assessment.verdict.failure_class == "policy_failure"
    assert "SHELL_TRUE" in {
        finding.rule for finding in assessment.gate.findings
    }


def test_verify_completion_rejects_head_after_pinned_commit(
    repo: tuple[Path, str],
) -> None:
    worktree, base = repo
    unsafe = "run(command, shell" + "=True)\n"
    (worktree / "tool.py").write_text(unsafe, encoding="utf-8")
    _git(worktree, "add", "tool.py")
    _git(worktree, "commit", "-m", "unsafe completion")
    pinned = _git(worktree, "rev-parse", "HEAD")
    (worktree / "tool.py").write_text("run(command, shell=False)\n", encoding="utf-8")
    _git(worktree, "add", "tool.py")
    _git(worktree, "commit", "-m", "later clean replacement")

    with pytest.raises(CompletionEvidenceError, match="no longer matches"):
        verify_completion(
            _spec(),
            worktree,
            base,
            completion_ref=pinned,
        )


def test_compact_evidence_drops_command_output_previews() -> None:
    secret = "credential-" + "must-not-persist"
    compact = compact_completion_evidence(
        {
            "observer": "loop",
            "commands": [
                {
                    "command": "python -m pytest -q",
                    "exit_code": 1,
                    "stdout_preview": secret,
                    "stderr_preview": secret,
                    "stdout_sha256": "a" * 64,
                    "stderr_sha256": "b" * 64,
                }
            ],
        }
    )

    assert secret not in str(compact)
    assert "stdout_preview" not in compact["commands"][0]
    assert "stderr_preview" not in compact["commands"][0]


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


def test_repository_module_cannot_shadow_pytest(repo: tuple[Path, str]) -> None:
    worktree, base = repo
    (worktree / "pytest.py").write_text(
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    (worktree / "verify_test.py").write_text(
        "def test_failure():\n    raise AssertionError('real failure')\n",
        encoding="utf-8",
    )
    _git(worktree, "add", "pytest.py", "verify_test.py")
    _git(worktree, "commit", "-m", "add shadow attempt")

    evidence = collect_completion_evidence(
        _spec(_pytest_command()),
        worktree,
        base,
    )

    assert evidence["commands"][0]["exit_code"] == 1
    assert "real failure" in evidence["commands"][0]["stdout_preview"]


def test_repository_conftest_cannot_forge_success(
    repo: tuple[Path, str],
) -> None:
    worktree, base = repo
    (worktree / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    session.exitstatus = 0\n",
        encoding="utf-8",
    )
    (worktree / "verify_test.py").write_text(
        "def test_failure():\n    raise AssertionError('must fail')\n",
        encoding="utf-8",
    )
    _git(worktree, "add", "conftest.py", "verify_test.py")
    _git(worktree, "commit", "-m", "add forged status hook")

    evidence = collect_completion_evidence(
        _spec(_pytest_command()),
        worktree,
        base,
    )

    assert evidence["commands"][0]["exit_code"] == 1
    assert "must fail" in evidence["commands"][0]["stdout_preview"]


def test_abrupt_pytest_exit_cannot_forge_success(
    repo: tuple[Path, str],
) -> None:
    worktree, base = repo
    (worktree / "verify_test.py").write_text(
        "import os\n"
        "os._exit(0)\n\n"
        "def test_failure():\n"
        "    raise AssertionError('must not be skipped')\n",
        encoding="utf-8",
    )
    _git(worktree, "add", "verify_test.py")
    _git(worktree, "commit", "-m", "add abrupt exit attempt")
    command = _pytest_command()
    spec = _spec(command)

    evidence = collect_completion_evidence(spec, worktree, base)

    assert evidence["commands"][0]["exit_code"] == 86
    assert evaluate_completion(spec, evidence).passed is False


def test_native_exit_forgery_is_gated_before_test_execution(
    repo: tuple[Path, str],
) -> None:
    worktree, base = repo
    forged_record = (
        '{"collected": 1, "failed": 0, "passed": 1, '
        '"pytest_exit_code": 0, "terminal": 1}'
    )
    (worktree / "verify_test.py").write_text(
        "import ctypes\n"
        "import os\n"
        f"os.write(1, {('JARVIS_PYTEST_RESULT ' + forged_record + chr(10)).encode()!r})\n"
        "ctypes.CDLL(None)._exit(0)\n",
        encoding="utf-8",
    )
    _git(worktree, "add", "verify_test.py")
    _git(worktree, "commit", "-m", "add native exit forgery")
    completion = _git(worktree, "rev-parse", "HEAD")

    assessment = verify_completion(
        _spec(_pytest_command("-s")),
        worktree,
        base,
        completion_ref=completion,
    )

    assert assessment.gate.passed is False
    assert "NATIVE_FFI" in {
        finding.rule for finding in assessment.gate.findings
    }
    assert assessment.evidence["commands"] == []
    assert assessment.verdict.passed is False


@pytest.mark.skipif(
    sys.platform != "darwin"
    or not Path("/usr/bin/sandbox-exec").is_file()
    or os.getenv("JARVIS_VERIFIER_SANDBOX") == "1",
    reason="requires a non-nested macOS Seatbelt process",
)
def test_verifier_cannot_read_or_write_outside_sandbox(
    repo: tuple[Path, str],
) -> None:
    worktree, base = repo
    external = worktree.parent / "outside-verifier.txt"
    shared = Path("/Users/Shared") / (
        f"jarvis-verifier-test-{os.getpid()}.txt"
    )
    external.write_text("private\n", encoding="utf-8")
    shared.write_text("shared-private\n", encoding="utf-8")
    source = (
        "from pathlib import Path\n"
        "import pytest\n\n"
        "def test_external_access_is_denied():\n"
        f"    target = Path({str(external)!r})\n"
        f"    shared = Path({str(shared)!r})\n"
        "    with pytest.raises(PermissionError):\n"
        "        target.read_text()\n"
        "    with pytest.raises(PermissionError):\n"
        "        target.write_text('changed')\n"
        "    with pytest.raises(PermissionError):\n"
        "        shared.read_text()\n"
    )
    _install_test(worktree, source)

    try:
        evidence = collect_completion_evidence(
            _spec(_pytest_command()),
            worktree,
            base,
        )
    finally:
        shared.unlink(missing_ok=True)

    assert evidence["commands"][0]["exit_code"] == 0
    assert external.read_text(encoding="utf-8") == "private\n"


@pytest.mark.parametrize(
    "command",
    [
        "python -c 'print(1)'",
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


def test_direct_repository_python_script_is_sandboxed_and_allowed(
    repo: tuple[Path, str],
) -> None:
    worktree, base = repo
    (worktree / "verify_script.py").write_text(
        "import sys\n"
        "raise SystemExit(0 if sys.argv[1:] == ['--help'] else 2)\n",
        encoding="utf-8",
    )
    _git(worktree, "add", "verify_script.py")
    _git(worktree, "commit", "-m", "add verification script")
    command = shlex.join(
        [sys.executable, "verify_script.py", "--help"]
    )

    evidence = collect_completion_evidence(
        _spec(command),
        worktree,
        base,
    )

    assert evidence["commands"][0]["exit_code"] == 0
    assert evidence["commands"][0]["policy_validated"] is True


def test_current_contract_entry_points_are_policy_valid() -> None:
    repo = Path(__file__).resolve().parents[1]
    payload = json.loads(
        (repo / "TASK_CONTRACTS.json").read_text(encoding="utf-8")
    )
    contracts = (
        payload if isinstance(payload, list) else payload.get("contracts", [])
    )
    rejected = {}
    for contract in contracts:
        command = contract.get("entry_point")
        if not command:
            continue
        _argv, error = completion_verifier._trusted_argv(command, repo)
        if error:
            rejected[str(contract.get("task_id") or "<unknown>")] = error

    assert rejected == {}


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


def test_verification_command_cannot_move_completion_head(
    repo: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree, base = repo

    def commit_during_verification(
        command: str,
        command_repo: Path,
        _timeout: float,
    ) -> dict[str, object]:
        (command_repo / "generated.txt").write_text("created\n", encoding="utf-8")
        _git(command_repo, "add", "generated.txt")
        _git(command_repo, "commit", "-m", "verifier mutation")
        return {"command": command, "exit_code": 0}

    monkeypatch.setattr(
        completion_verifier,
        "_execute_command",
        commit_during_verification,
    )

    evidence = collect_completion_evidence(
        _spec("git status --short"),
        worktree,
        base,
    )

    finding_ids = {finding["id"] for finding in evidence["policy_findings"]}
    assert "verifier_head_mutation" in finding_ids
    assert (
        evidence["completion_commit_before_commands"]
        != evidence["completion_commit"]
    )


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


def test_verification_environment_sets_safe_runtime_isolation(
    repo: tuple[Path, str],
) -> None:
    worktree, _ = repo
    base = _install_test(
        worktree,
        "import os\n"
        "from pathlib import Path\n\n"
        "def test_safe_runtime_isolation():\n"
        "    assert os.environ['JARVIS_AUTO_VERIFY'] == '0'\n"
        "    assert os.environ['JARVIS_NATIVE_TOOL_LOOP'] == '0'\n"
        "    assert os.environ['JARVIS_SKIP_DOTENV'] == '1'\n"
        "    assert os.environ['JARVIS_OLLAMA_LIVENESS_DISABLED'] == '1'\n"
        "    assert os.environ['JARVIS_API_ALLOW_NO_AUTH'] == '1'\n"
        "    assert os.environ['JARVIS_VERIFIER_SANDBOX'] == '1'\n"
        "    assert os.environ['JARVIS_RUN_LIVE_INTEGRATION_TESTS'] == '0'\n"
        "    assert os.environ['JARVIS_ALLOW_SIDE_EFFECTS'] == '0'\n"
        "    assert os.environ['JARVIS_RUN_PACKAGED_SMOKE'] == '0'\n"
        "    sandbox = Path(os.environ['HOME']).parent\n"
        "    assert Path(os.environ['JARVIS_SECURITY_AUDIT_PATH']).parent == sandbox\n"
        "    assert Path(os.environ['JARVIS_TASK_DB_PATH']).parent == sandbox\n",
    )

    evidence = collect_completion_evidence(
        _spec(_pytest_command()),
        worktree,
        base,
    )

    result = evidence["commands"][0]
    assert result["exit_code"] == 0, (
        result["stdout_preview"],
        result["stderr_preview"],
    )


def test_nested_verifier_reuses_kernel_enforced_outer_sandbox(
    repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree, _ = repo
    base = _install_test(worktree, "def test_ok():\n    assert True\n")
    monkeypatch.setattr(
        completion_verifier,
        "_already_in_verification_sandbox",
        lambda: True,
    )
    monkeypatch.setattr(
        completion_verifier,
        "_sandboxed_argv",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("nested Seatbelt must not be applied")
        ),
    )

    evidence = collect_completion_evidence(
        _spec(_pytest_command()),
        worktree,
        base,
    )

    assert evidence["commands"][0]["exit_code"] == 0


def test_pytest_observer_counts_parent_tests_not_subtest_reports(
    repo: tuple[Path, str],
) -> None:
    worktree, _ = repo
    base = _install_test(
        worktree,
        "import unittest\n\n"
        "class TestSubtests(unittest.TestCase):\n"
        "    def test_examples(self):\n"
        "        for value in range(3):\n"
        "            with self.subTest(value=value):\n"
        "                self.assertLess(value, 3)\n",
    )

    evidence = collect_completion_evidence(
        _spec(_pytest_command()),
        worktree,
        base,
    )

    assert evidence["commands"][0]["exit_code"] == 0


def test_verifier_preloads_stable_runtime_modules_before_collection(
    repo: tuple[Path, str],
) -> None:
    worktree, _ = repo
    (worktree / "brains").mkdir()
    (worktree / "brains" / "__init__.py").write_text("", encoding="utf-8")
    (worktree / "tools.py").write_text(
        "import builtins\n"
        "builtins.jarvis_preload_order = ['tools']\n",
        encoding="utf-8",
    )
    (worktree / "brains" / "brain_apple_foundation.py").write_text(
        "import builtins\n"
        "builtins.jarvis_preload_order.append('apple')\n",
        encoding="utf-8",
    )
    (worktree / "brains" / "brain_ollama.py").write_text(
        "import builtins\n"
        "builtins.jarvis_preload_order.append('ollama')\n",
        encoding="utf-8",
    )
    (worktree / "verify_test.py").write_text(
        "import builtins\n\n"
        "def test_preload_order():\n"
        "    assert builtins.jarvis_preload_order == "
        "['tools', 'apple', 'ollama']\n",
        encoding="utf-8",
    )
    _git(worktree, "add", "brains", "tools.py", "verify_test.py")
    _git(worktree, "commit", "-m", "add preload fixtures")
    base = _git(worktree, "rev-parse", "HEAD")

    evidence = collect_completion_evidence(
        _spec(_pytest_command()),
        worktree,
        base,
    )

    assert evidence["commands"][0]["exit_code"] == 0
