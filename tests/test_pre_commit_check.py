from __future__ import annotations

from pathlib import Path

from harness import pre_commit_check


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_clean_file_passes(tmp_path: Path):
    source = _write(tmp_path / "clean.py", "value = 1\n")

    result = pre_commit_check.run_checks([source])

    assert result.passed is True
    assert result.files_checked == [str(source)]
    assert result.findings == []
    assert result.syntax_errors == []


def test_each_security_rule_is_reported(tmp_path: Path):
    source = _write(
        tmp_path / "unsafe.py",
        "\n".join(
            [
                "run(" + "shell=True)",  # pre-commit-ok
                "ev" + "al('input')",
                "API_" + "KEY = 'secret-value'",
                "pickle." + "load(stream)",
            ]
        ),
    )

    result = pre_commit_check.run_checks([source])

    assert result.passed is False
    assert {finding.rule for finding in result.findings} == {
        "SHELL_TRUE",
        "EVAL_EXEC",
        "HARDCODED_SECRET",
        "UNSAFE_DESERIALIZE",
    }


def test_inline_suppression_and_env_lookup_are_allowed(tmp_path: Path):
    source = _write(
        tmp_path / "allowed.py",
        "\n".join(
            [
                "ev" + "al('trusted')  # pre-commit-ok",
                "API_" + "KEY = os.getenv('API_KEY')",
            ]
        ),
    )

    result = pre_commit_check.run_checks([source])

    assert result.passed is True


def test_syntax_error_fails_gate(tmp_path: Path):
    source = _write(tmp_path / "broken.py", "def broken(:\n")

    result = pre_commit_check.run_checks([source])

    assert result.passed is False
    assert result.syntax_errors


def test_missing_file_fails_gate(tmp_path: Path):
    missing = tmp_path / "missing.py"

    result = pre_commit_check.run_checks([missing])

    assert result.passed is False
    assert result.findings[0].rule == "FILE_NOT_FOUND"


def test_main_accepts_explicit_argument_list(tmp_path: Path, capsys):
    source = _write(tmp_path / "clean.py", "value = 1\n")

    exit_code = pre_commit_check.main([str(source)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "REVIEW gate PASSED" in captured.out


def test_main_without_arguments_returns_usage_error(capsys):
    exit_code = pre_commit_check.main([])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Usage:" in captured.err
