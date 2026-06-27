"""Tests for tools/git_ops.py — all subprocess calls mocked."""
from __future__ import annotations

from unittest.mock import patch, MagicMock
import subprocess

import tools.git_ops as git_ops


def _mock_run(stdout: str = "", stderr: str = "", returncode: int = 0):
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


def _patch_run(stdout="", stderr="", returncode=0):
    return patch(
        "subprocess.run",
        return_value=_mock_run(stdout, stderr, returncode),
    )


# ── Read ops ─────────────────────────────────────────────────────────────────

class TestGitStatus:
    def test_returns_short_status(self):
        with _patch_run("M foo.py"):
            assert git_ops.git_status() == "M foo.py"

    def test_clean_tree_returns_message(self):
        with _patch_run(""):
            assert git_ops.git_status() == "Working tree is clean."

    def test_subprocess_failure_returns_error(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            out = git_ops.git_status()
        assert "failed" in out.lower() or "not found" in out.lower()


class TestGitDiff:
    def test_returns_diff_output(self):
        with _patch_run("diff content here"):
            out = git_ops.git_diff()
        assert out == "diff content here"

    def test_empty_diff_returns_no_diff(self):
        with _patch_run(""):
            assert git_ops.git_diff() == "No diff."

    def test_staged_flag_passes_cached(self):
        with patch("subprocess.run", return_value=_mock_run("staged")) as mock_run:
            git_ops.git_diff(staged=True)
        args = mock_run.call_args[0][0]
        assert "--cached" in args

    def test_path_is_passed_to_subprocess(self):
        with patch("subprocess.run", return_value=_mock_run("")) as mock_run:
            git_ops.git_diff(path="foo.py")
        args = mock_run.call_args[0][0]
        assert "foo.py" in " ".join(args)


class TestGitLog:
    def test_returns_log(self):
        with _patch_run("abc1234 fix bug"):
            out = git_ops.git_log(n=5)
        assert "abc1234" in out

    def test_n_is_capped_at_50(self):
        with patch("subprocess.run", return_value=_mock_run("")) as mock_run:
            git_ops.git_log(n=999)
        args = mock_run.call_args[0][0]
        assert "-50" in args

    def test_oneline_flag(self):
        with patch("subprocess.run", return_value=_mock_run("")) as mock_run:
            git_ops.git_log(oneline=True)
        args = mock_run.call_args[0][0]
        assert "--oneline" in args


class TestGitBranch:
    def test_returns_branch_list(self):
        with _patch_run("* main abc1234\n  dev  def5678"):
            out = git_ops.git_branch()
        assert "main" in out


class TestGitShow:
    def test_returns_show_output(self):
        with _patch_run("commit abc123\nAuthor: X\n"):
            out = git_ops.git_show("abc123")
        assert "commit" in out

    def test_rejects_invalid_ref(self):
        out = git_ops.git_show("$(rm -rf /)")
        assert "Invalid ref" in out

    def test_defaults_to_head(self):
        with patch("subprocess.run", return_value=_mock_run("")) as mock_run:
            git_ops.git_show()
        args = mock_run.call_args[0][0]
        assert "HEAD" in args


# ── Write ops ─────────────────────────────────────────────────────────────────

class TestGitAdd:
    def test_stages_file(self):
        with _patch_run(""):
            out = git_ops.git_add(["README.md"])
        assert "README.md" in out

    def test_rejects_bare_dot(self):
        out = git_ops.git_add(["."])
        assert "Rejected" in out or "not allowed" in out

    def test_rejects_dotdot(self):
        out = git_ops.git_add([".."])
        assert "Rejected" in out or "not allowed" in out

    def test_rejects_path_traversal(self):
        out = git_ops.git_add(["../../etc/passwd"])
        assert "Rejected" in out or "traversal" in out

    def test_empty_paths_returns_error(self):
        out = git_ops.git_add([])
        assert "No paths" in out

    def test_subprocess_failure_reported(self):
        with patch("subprocess.run", return_value=_mock_run("", "error: pathspec", 1)):
            out = git_ops.git_add(["nonexistent.py"])
        assert "failed" in out.lower() or "error" in out.lower()


class TestGitCommit:
    def test_commits_with_valid_message(self):
        with _patch_run("[main abc123] my commit"):
            out = git_ops.git_commit("my commit message here")
        assert "abc123" in out or "commit" in out.lower()

    def test_rejects_short_message(self):
        out = git_ops.git_commit("hi")
        assert "short" in out.lower() or "minimum" in out.lower()

    def test_rejects_empty_message(self):
        out = git_ops.git_commit("")
        assert "short" in out.lower() or "minimum" in out.lower()

    def test_rejects_shell_injection_chars(self):
        out = git_ops.git_commit("fix; rm -rf /")
        assert "disallowed" in out.lower() or "shell" in out.lower()

    def test_rejects_backtick_injection(self):
        out = git_ops.git_commit("fix `whoami`")
        assert "disallowed" in out.lower()


# ── dispatch ─────────────────────────────────────────────────────────────────

class TestDispatch:
    def test_status_action(self):
        with _patch_run("M x.py"):
            ok, out = git_ops.dispatch("status", {})
        assert ok is True
        assert "M x.py" in out

    def test_unknown_action(self):
        ok, out = git_ops.dispatch("push", {})
        assert ok is False
        assert "push" in out.lower() or "unknown" in out.lower()

    def test_diff_routes_correctly(self):
        with _patch_run("diff here"):
            ok, out = git_ops.dispatch("diff", {"staged": "false"})
        assert ok is True

    def test_commit_via_dispatch(self):
        with _patch_run("[main abc123]"):
            ok, out = git_ops.dispatch("commit", {"message": "fix the thing properly"})
        assert ok is True

    def test_add_parses_comma_separated_paths(self):
        with _patch_run(""):
            ok, out = git_ops.dispatch("add", {"paths": "foo.py, bar.py"})
        assert ok is True
        assert "foo.py" in out
