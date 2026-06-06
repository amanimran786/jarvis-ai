"""
Tests for the ADE (Autonomous Dev Environment) package.

All subprocess, filesystem, and tmux calls are mocked.
"""
from __future__ import annotations

import json
import sys
import time
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ── Ensure ade package is importable ─────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def repo(tmp_path):
    """Minimal fake repo root: initialise git so commands don't error."""
    subprocess_mod = __import__("subprocess")
    subprocess_mod.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess_mod.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=str(tmp_path),
        capture_output=True,
        env={**__import__("os").environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )
    return tmp_path


# ── ade.state ─────────────────────────────────────────────────────────────────

class TestState:
    def test_upsert_and_load(self, repo):
        from ade import state as st
        st.upsert(repo, "my-task", status="PLANNING", prompt="do stuff")
        data = st.load(repo)
        assert "my-task" in data
        assert data["my-task"]["status"] == "PLANNING"
        assert data["my-task"]["prompt"] == "do stuff"

    def test_set_status(self, repo):
        from ade import state as st
        st.upsert(repo, "t1", status="PLANNING")
        st.set_status(repo, "t1", "EXECUTING")
        assert st.get(repo, "t1")["status"] == "EXECUTING"

    def test_remove(self, repo):
        from ade import state as st
        st.upsert(repo, "t2", status="DONE")
        st.remove(repo, "t2")
        assert st.get(repo, "t2") is None

    def test_load_missing_file_returns_empty(self, repo):
        from ade import state as st
        assert st.load(repo / "nonexistent_parent") == {}

    def test_upsert_merges_fields(self, repo):
        from ade import state as st
        st.upsert(repo, "t3", status="PLANNING", retries=0)
        st.upsert(repo, "t3", retries=2)
        entry = st.get(repo, "t3")
        assert entry["retries"] == 2
        assert entry["status"] == "PLANNING"  # not overwritten


# ── ade.notify ────────────────────────────────────────────────────────────────

class TestNotify:
    def test_send_calls_osascript_on_darwin(self):
        from ade import notify
        with patch("platform.system", return_value="Darwin"), \
             patch("subprocess.run") as mock_run:
            notify.send("Title", "Message")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "osascript"

    def test_send_calls_notify_send_on_linux(self):
        from ade import notify
        with patch("platform.system", return_value="Linux"), \
             patch("subprocess.run") as mock_run:
            notify.send("Title", "Message")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "notify-send"

    def test_send_does_not_raise_on_failure(self):
        from ade import notify
        with patch("platform.system", return_value="Darwin"), \
             patch("subprocess.run", side_effect=OSError("no osascript")):
            notify.send("T", "M")  # must not raise


# ── ade.session ───────────────────────────────────────────────────────────────

class TestSession:
    def test_exists_true_when_returncode_zero(self):
        from ade import session
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            assert session.exists("ade-foo") is True

    def test_exists_false_when_returncode_nonzero(self):
        from ade import session
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            assert session.exists("ade-foo") is False

    def test_list_sessions_parses_output(self):
        from ade import session
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "ade-task1\nade-task2\n"
            result = session.list_sessions()
        assert result == ["ade-task1", "ade-task2"]

    def test_list_sessions_empty_on_failure(self):
        from ade import session
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            assert session.list_sessions() == []

    def test_create_raises_if_tmux_missing(self, tmp_path):
        from ade import session
        with patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="tmux not found"):
                session.create("ade-t", tmp_path, tmp_path / "loop", "t", "p", tmp_path)

    def test_create_raises_if_session_exists(self, tmp_path):
        from ade import session
        with patch("shutil.which", return_value="/usr/bin/tmux"), \
             patch.object(session, "exists", return_value=True):
            with pytest.raises(RuntimeError, match="already exists"):
                session.create("ade-t", tmp_path, tmp_path / "loop", "t", "p", tmp_path)

    def test_create_calls_tmux_new_session(self, tmp_path):
        from ade import session
        with patch("shutil.which", return_value="/usr/bin/tmux"), \
             patch.object(session, "exists", return_value=False), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            session.create("ade-t", tmp_path, tmp_path / "loop", "t", "my prompt", tmp_path)
        cmd = mock_run.call_args[0][0]
        assert cmd[:3] == ["tmux", "new-session", "-d"]
        assert "ade-t" in cmd
        assert "my prompt" in cmd


# ── ade.loop — test detection ─────────────────────────────────────────────────

class TestDetectTestCmd:
    def test_detects_npm_test(self, tmp_path):
        from ade.loop import detect_test_cmd
        (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
        assert detect_test_cmd(tmp_path) == ["npm", "test"]

    def test_skips_npm_if_no_test_script(self, tmp_path):
        from ade.loop import detect_test_cmd
        (tmp_path / "package.json").write_text('{"scripts": {"build": "webpack"}}')
        (tmp_path / "pytest.ini").write_text("")
        assert detect_test_cmd(tmp_path)[0:2] == [sys.executable, "-m"]

    def test_detects_makefile_test(self, tmp_path):
        from ade.loop import detect_test_cmd
        (tmp_path / "Makefile").write_text("\ntest:\n\tpytest\n")
        assert detect_test_cmd(tmp_path) == ["make", "test"]

    def test_detects_pytest_via_ini(self, tmp_path):
        from ade.loop import detect_test_cmd
        (tmp_path / "pytest.ini").write_text("")
        cmd = detect_test_cmd(tmp_path)
        assert cmd[1:3] == ["-m", "pytest"]

    def test_detects_pytest_via_tests_dir(self, tmp_path):
        from ade.loop import detect_test_cmd
        (tmp_path / "tests").mkdir()
        cmd = detect_test_cmd(tmp_path)
        assert "pytest" in cmd

    def test_detects_cargo_test(self, tmp_path):
        from ade.loop import detect_test_cmd
        (tmp_path / "Cargo.toml").write_text("[package]")
        assert detect_test_cmd(tmp_path) == ["cargo", "test"]

    def test_returns_none_when_undetectable(self, tmp_path):
        from ade.loop import detect_test_cmd
        assert detect_test_cmd(tmp_path) is None


# ── ade.loop — phase_verify ───────────────────────────────────────────────────

class TestPhaseVerify:
    def test_returns_true_on_pass(self, tmp_path, repo):
        from ade.loop import phase_verify
        (tmp_path / "pytest.ini").write_text("")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "5 passed"
            mock_run.return_value.stderr = ""
            passed, errors = phase_verify("task", tmp_path, repo)
        assert passed is True
        assert errors == ""

    def test_returns_false_on_fail(self, tmp_path, repo):
        from ade.loop import phase_verify
        (tmp_path / "pytest.ini").write_text("")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = "FAILED test_foo"
            passed, errors = phase_verify("task", tmp_path, repo)
        assert passed is False
        assert "FAILED" in errors

    def test_returns_true_when_no_test_suite(self, tmp_path, repo):
        from ade.loop import phase_verify
        passed, errors = phase_verify("task", tmp_path, repo)
        assert passed is True

    def test_handles_timeout(self, tmp_path, repo):
        from ade.loop import phase_verify
        import subprocess
        (tmp_path / "pytest.ini").write_text("")
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("pytest", 600)):
            passed, errors = phase_verify("task", tmp_path, repo)
        assert passed is False
        assert "timed out" in errors


class TestClaudePromptCommand:
    def test_claude_prompt_cmd_is_permission_safe_by_default(self, monkeypatch):
        from ade.loop import _claude_prompt_cmd
        monkeypatch.delenv("ADE_CLAUDE_SKIP_PERMISSIONS", raising=False)
        cmd = _claude_prompt_cmd("plan")
        assert cmd == ["claude", "-p", "plan"]

    def test_claude_prompt_cmd_allows_explicit_skip_permissions_opt_in(self, monkeypatch):
        from ade.loop import _claude_prompt_cmd
        monkeypatch.setenv("ADE_CLAUDE_SKIP_PERMISSIONS", "1")
        cmd = _claude_prompt_cmd("plan")
        assert cmd == ["claude", "--dangerously-skip-permissions", "-p", "plan"]


# ── ade.cli ───────────────────────────────────────────────────────────────────

class TestCli:
    def test_list_prints_no_tasks_when_empty(self, repo, capsys):
        from ade.cli import cmd_list
        with patch("ade.cli._repo_root", return_value=repo), \
             patch("ade.session.list_sessions", return_value=[]):
            cmd_list()
        out = capsys.readouterr().out
        assert "No active tasks" in out

    def test_list_shows_task_entries(self, repo, capsys):
        from ade import state as st
        from ade.cli import cmd_list
        st.upsert(repo, "refactor-login", status="EXECUTING", session="ade-refactor-login", prompt="Refactor")
        with patch("ade.cli._repo_root", return_value=repo), \
             patch("ade.session.list_sessions", return_value=["ade-refactor-login"]):
            cmd_list()
        out = capsys.readouterr().out
        assert "refactor-login" in out
        assert "EXECUTING" in out

    def test_watch_exits_if_session_missing(self, repo):
        from ade.cli import cmd_watch
        with patch("ade.cli._repo_root", return_value=repo), \
             patch("ade.session.exists", return_value=False), \
             pytest.raises(SystemExit):
            cmd_watch("missing-task")

    def test_stop_kills_session_and_removes_state(self, repo):
        from ade import state as st
        from ade.cli import cmd_stop
        worktree = repo / ".worktrees" / "my-task"
        st.upsert(repo, "my-task", session="ade-my-task", worktree_path=str(worktree))
        with patch("ade.cli._repo_root", return_value=repo), \
             patch("ade.session.kill") as mock_kill, \
             patch("subprocess.run") as mock_sub:
            mock_sub.return_value.returncode = 0
            cmd_stop("my-task")
        mock_kill.assert_called_once_with("ade-my-task")
        assert st.get(repo, "my-task") is None

    def test_sync_handles_merge_conflict(self, repo, capsys):
        from ade import state as st
        from ade.cli import cmd_sync
        worktree = repo / ".worktrees" / "task-x"
        worktree.mkdir(parents=True)
        st.upsert(repo, "task-x", branch="ade/task-x", worktree_path=str(worktree))

        def fake_run(cmd, **kw):
            r = MagicMock()
            if "merge" in cmd:
                r.returncode = 1
                r.stdout = "CONFLICT (content): Merge conflict in foo.py"
                r.stderr = ""
            else:
                r.returncode = 0
                r.stdout = ""
                r.stderr = ""
            return r

        with patch("ade.cli._repo_root", return_value=repo), \
             patch("subprocess.run", side_effect=fake_run), \
             patch("ade.notify.send") as mock_notify:
            cmd_sync("task-x")

        out = capsys.readouterr().out
        assert "conflict" in out.lower()
        mock_notify.assert_called_once()
        assert "conflict" in mock_notify.call_args[0][0].lower() or \
               "conflict" in mock_notify.call_args[0][1].lower()
