from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.commit_review_gate import run_commit_gate, write_gate_log


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
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "gate@example.com")
    _git(root, "config", "user.name", "Commit Gate Test")
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", ".gitignore", "base.txt")
    _git(root, "commit", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD")


def _commit(repo: Path, path: str, source: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-m", f"update {path}")
    return _git(repo, "rev-parse", "HEAD")


def test_clean_committed_python_blob_passes(repo: tuple[Path, str]) -> None:
    root, base = repo
    completion = _commit(root, "tool.py", "VALUE = 1\n")

    result = run_commit_gate(root, base, completion)

    assert result.passed is True
    assert result.files_checked == ["tool.py"]
    assert result.findings == []


def test_unsafe_commit_cannot_hide_behind_clean_worktree_replacement(
    repo: tuple[Path, str],
) -> None:
    root, base = repo
    unsafe = "run(command, shell" + "=True)\n"
    completion = _commit(root, "tool.py", unsafe)
    (root / "tool.py").write_text("run(command, shell=False)\n", encoding="utf-8")

    result = run_commit_gate(root, base, completion)

    rules = {finding.rule for finding in result.findings}
    assert result.passed is False
    assert {"DIRTY_WORKTREE", "SHELL_TRUE"} <= rules


def test_new_inline_suppression_is_not_self_authorizing(
    repo: tuple[Path, str],
) -> None:
    root, base = repo
    suppressed = "run(command, shell=" + "True)  # pre-commit-ok\n"
    completion = _commit(root, "tool.py", suppressed)

    result = run_commit_gate(root, base, completion)

    assert result.passed is False
    assert "UNAUTHORIZED_SUPPRESSION" in {
        finding.rule for finding in result.findings
    }


def test_preexisting_suppression_is_not_reclassified(
    repo: tuple[Path, str],
) -> None:
    root, _ = repo
    existing = "ev" + "al('trusted')  # pre-commit-ok\n"
    base = _commit(root, "tool.py", existing)
    completion = _commit(root, "tool.py", existing + "VALUE = 1\n")

    result = run_commit_gate(root, base, completion)

    assert result.passed is True


def test_python_symlink_commit_is_rejected(repo: tuple[Path, str]) -> None:
    root, base = repo
    (root / "target.txt").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "linked.py").symlink_to("target.txt")
    _git(root, "add", "target.txt", "linked.py")
    _git(root, "commit", "-m", "add Python symlink")
    completion = _git(root, "rev-parse", "HEAD")

    result = run_commit_gate(root, base, completion)

    assert result.passed is False
    assert "UNSAFE_GIT_MODE" in {finding.rule for finding in result.findings}


def test_deleted_python_file_does_not_require_blob_scan(
    repo: tuple[Path, str],
) -> None:
    root, _ = repo
    base = _commit(root, "obsolete.py", "VALUE = 1\n")
    (root / "obsolete.py").unlink()
    _git(root, "add", "obsolete.py")
    _git(root, "commit", "-m", "remove obsolete Python")
    completion = _git(root, "rev-parse", "HEAD")

    result = run_commit_gate(root, base, completion)

    assert result.passed is True
    assert result.files_checked == []


def test_findings_and_log_never_contain_secret_source(
    repo: tuple[Path, str],
    tmp_path: Path,
) -> None:
    root, base = repo
    secret = "credential-" + "must-not-leak"
    name = "API" + "_KEY"
    completion = _commit(root, "tool.py", f"{name} = {secret!r}\n")

    result = run_commit_gate(root, base, completion)
    log_path = tmp_path / "logs" / "violations.log"
    write_gate_log(
        log_path,
        "TASK-1",
        "session-1",
        result,
        timestamp="2026-07-23T12:00:00+00:00",
    )

    assert result.passed is False
    assert secret not in "\n".join(result.reasons())
    assert secret not in log_path.read_text(encoding="utf-8")
    assert log_path.stat().st_mode & 0o777 == 0o600


def test_completion_ref_must_still_be_worktree_head(
    repo: tuple[Path, str],
) -> None:
    root, base = repo
    completion = _commit(root, "tool.py", "VALUE = 1\n")
    _commit(root, "later.py", "VALUE = 2\n")

    result = run_commit_gate(root, base, completion)

    assert result.passed is False
    assert "HEAD_MISMATCH" in {finding.rule for finding in result.findings}


def test_unknown_commit_fails_closed(repo: tuple[Path, str]) -> None:
    root, base = repo

    result = run_commit_gate(root, base, "f" * 40)

    assert result.passed is False
    assert [finding.rule for finding in result.findings] == [
        "INVALID_GIT_EVIDENCE"
    ]


def test_non_ancestor_commit_range_is_rejected(repo: tuple[Path, str]) -> None:
    root, base = repo
    unrelated_base = _commit(root, "main.py", "VALUE = 1\n")
    _git(root, "checkout", "--detach", base)
    completion = _commit(root, "other.py", "VALUE = 2\n")

    result = run_commit_gate(root, unrelated_base, completion)

    assert result.passed is False
    assert "BASE_NOT_ANCESTOR" in {
        finding.rule for finding in result.findings
    }


def test_syntax_invalid_committed_blob_is_rejected(
    repo: tuple[Path, str],
) -> None:
    root, base = repo
    completion = _commit(root, "broken.py", "def broken(:\n")

    result = run_commit_gate(root, base, completion)

    assert result.passed is False
    assert "SYNTAX_ERROR" in {finding.rule for finding in result.findings}


@pytest.mark.parametrize(
    "source,expected_rule",
    [
        (
            "import subprocess\nsubprocess.run(command, shell=(True))\n",
            "SHELL_TRUE",
        ),
        (
            "import subprocess\nsubprocess.run(command, shell=\n    True)\n",
            "SHELL_TRUE",
        ),
        (
            'import subprocess\nsubprocess.run(command, **{"shell": True})\n',
            "SHELL_TRUE",
        ),
        (
            "import subprocess\noptions = {}\nsubprocess.run(command, **options)\n",
            "DYNAMIC_SUBPROCESS_KWARGS",
        ),
        (
            "import subprocess\n"
            "subprocess.Popen(command, -1, None, None, None, None, None, True, True)\n",
            "SHELL_TRUE",
        ),
        (
            "import subprocess\nsubprocess.getoutput(command)\n",
            "SHELL_API",
        ),
        (
            "import functools\nimport subprocess\n"
            "options = {}\n"
            "runner = functools.partial(subprocess.run, **options)\n",
            "DYNAMIC_SUBPROCESS_KWARGS",
        ),
        (
            "import functools\nimport subprocess\n"
            "runner = functools.partial(subprocess.getoutput, command)\n",
            "SHELL_API",
        ),
        (
            "import functools\nimport subprocess\n"
            "runner = functools.partial("
            "subprocess.Popen, command, -1, None, None, None, None, None, True, True"
            ")\n",
            "SHELL_TRUE",
        ),
        (
            "import os\nos._exit(0)\n",
            "HARD_PROCESS_EXIT",
        ),
        (
            "import ctypes\nctypes.CDLL(None)\n",
            "NATIVE_FFI",
        ),
        (
            "import os\nos.execv('/usr/bin/true', ['true'])\n",
            "PROCESS_REPLACEMENT",
        ),
        (
            "import importlib\nimportlib.import_module('ctypes')\n",
            "NATIVE_FFI",
        ),
    ],
)
def test_ast_gate_blocks_shell_syntax_bypasses(
    repo: tuple[Path, str],
    source: str,
    expected_rule: str,
) -> None:
    root, base = repo
    completion = _commit(root, "tool.py", source)

    result = run_commit_gate(root, base, completion)

    assert result.passed is False
    assert expected_rule in {finding.rule for finding in result.findings}


def test_python_windowed_script_is_scanned(repo: tuple[Path, str]) -> None:
    root, base = repo
    source = "import subprocess\nsubprocess.run(command, shell=(True))\n"
    completion = _commit(root, "tool.pyw", source)

    result = run_commit_gate(root, base, completion)

    assert result.passed is False
    assert "SHELL_TRUE" in {finding.rule for finding in result.findings}


def test_executable_python_shebang_script_is_scanned(
    repo: tuple[Path, str],
) -> None:
    root, base = repo
    script = root / "worker"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess\n"
        "subprocess.run(command, shell=(True))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    _git(root, "add", "worker")
    _git(root, "commit", "-m", "add executable worker")
    completion = _git(root, "rev-parse", "HEAD")

    result = run_commit_gate(root, base, completion)

    assert result.passed is False
    assert "SHELL_TRUE" in {finding.rule for finding in result.findings}


def test_control_characters_in_git_path_are_log_escaped(
    repo: tuple[Path, str],
) -> None:
    root, base = repo
    path = "odd\nname.py"
    completion = _commit(
        root,
        path,
        "import subprocess\nsubprocess.run(command, shell=(True))\n",
    )

    result = run_commit_gate(root, base, completion)

    reasons = result.reasons()
    assert result.passed is False
    assert all("\n" not in reason for reason in reasons)
    assert any("\\n" in reason for reason in reasons)


def test_gate_log_rejects_symlink_target(
    repo: tuple[Path, str],
    tmp_path: Path,
) -> None:
    root, base = repo
    unsafe = "run(command, shell" + "=True)\n"
    completion = _commit(root, "tool.py", unsafe)
    result = run_commit_gate(root, base, completion)
    external = tmp_path / "external.txt"
    external.write_text("preserve\n", encoding="utf-8")
    external.chmod(0o644)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "violations.log").symlink_to(external)

    with pytest.raises(OSError):
        write_gate_log(
            log_dir / "violations.log",
            "TASK-1",
            "session-1",
            result,
            timestamp="2026-07-23T12:00:00+00:00",
        )

    assert external.read_text(encoding="utf-8") == "preserve\n"
    assert external.stat().st_mode & 0o777 == 0o644


def test_gate_log_escapes_control_characters_in_header(
    repo: tuple[Path, str],
    tmp_path: Path,
) -> None:
    root, base = repo
    unsafe = "run(command, shell" + "=True)\n"
    completion = _commit(root, "tool.py", unsafe)
    result = run_commit_gate(root, base, completion)
    log_path = tmp_path / "logs" / "violations.log"

    write_gate_log(
        log_path,
        "TASK-1\nforged",
        "session-1\rforged",
        result,
        timestamp="2026-07-23T12:00:00+00:00\nforged",
    )

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 + len(result.reasons())
    assert "\\nforged" in lines[0]
    assert "\\rforged" in lines[0]
