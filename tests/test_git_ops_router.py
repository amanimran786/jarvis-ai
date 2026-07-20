"""
Tests for agentic git operations in router.py and tools/git_ops.py.

Covers: detection, dispatch, commit message parsing, cancel command,
operative cancel_event propagation, and route_stream integration.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.git_ops import (
    _safe_path,
    dispatch,
    git_add_all,
    git_commit,
    git_diff,
    git_log,
    git_status,
)
from router import (
    _is_cancel_task_command,
    _is_git_commit_query,
    _is_git_diff_query,
    _is_git_log_query,
    _is_git_status_query,
    _parse_git_commit_message,
)


# ── git_status detection ───────────────────────────────────────────────────────

class TestGitStatusDetection:
    def test_git_status_literal(self):
        assert _is_git_status_query("git status") is True

    def test_what_files_changed(self):
        assert _is_git_status_query("what files changed") is True

    def test_what_is_modified(self):
        assert _is_git_status_query("what is modified") is True

    def test_show_modified_files(self):
        assert _is_git_status_query("show modified files") is True

    def test_not_triggered_by_unrelated(self):
        assert _is_git_status_query("what's the weather") is False


# ── git_diff detection ─────────────────────────────────────────────────────────

class TestGitDiffDetection:
    def test_git_diff_literal(self):
        assert _is_git_diff_query("git diff") is True

    def test_show_the_diff(self):
        assert _is_git_diff_query("show me the diff") is True

    def test_what_did_i_change(self):
        assert _is_git_diff_query("what did i change") is True

    def test_show_my_changes(self):
        assert _is_git_diff_query("show my changes") is True

    def test_not_triggered_by_unrelated(self):
        assert _is_git_diff_query("show my calendar") is False


# ── git_log detection ──────────────────────────────────────────────────────────

class TestGitLogDetection:
    def test_git_log_literal(self):
        assert _is_git_log_query("git log") is True

    def test_show_recent_commits(self):
        assert _is_git_log_query("show recent commits") is True

    def test_commit_history(self):
        assert _is_git_log_query("commit history") is True

    def test_last_5_commits(self):
        assert _is_git_log_query("last 5 commits") is True

    def test_not_triggered_by_unrelated(self):
        assert _is_git_log_query("show my tasks") is False


# ── git_commit detection ───────────────────────────────────────────────────────

class TestGitCommitDetection:
    def test_git_commit_literal(self):
        assert _is_git_commit_query("git commit -m add feature") is True

    def test_commit_my_changes(self):
        assert _is_git_commit_query("commit my changes with message add tests") is True

    def test_commit_these_changes(self):
        assert _is_git_commit_query("commit these changes as fix the bug") is True

    def test_stage_and_commit(self):
        assert _is_git_commit_query("stage and commit") is True

    def test_push_excluded(self):
        assert _is_git_commit_query("commit and push my changes") is False

    def test_not_triggered_by_unrelated(self):
        assert _is_git_commit_query("what are my tasks") is False


# ── commit message parsing ─────────────────────────────────────────────────────

class TestParseGitCommitMessage:
    def test_with_message_keyword(self):
        msg = _parse_git_commit_message("commit my changes with message add unit tests")
        assert msg == "add unit tests"

    def test_git_commit_m(self):
        msg = _parse_git_commit_message("git commit -m fix the login bug")
        assert msg == "fix the login bug"

    def test_commit_as(self):
        msg = _parse_git_commit_message("commit these changes as refactor auth module")
        assert msg == "refactor auth module"

    def test_quoted_message(self):
        msg = _parse_git_commit_message('commit my changes with message "update config"')
        assert msg == "update config"

    def test_too_short_returns_none(self):
        assert _parse_git_commit_message("commit as fix") is None

    def test_no_message_returns_none(self):
        assert _parse_git_commit_message("commit my changes") is None


# ── cancel detection ───────────────────────────────────────────────────────────

class TestCancelDetection:
    def test_slash_cancel(self):
        assert _is_cancel_task_command("/cancel") is True

    def test_slash_cancel_task(self):
        assert _is_cancel_task_command("/cancel task") is True

    def test_cancel_task(self):
        assert _is_cancel_task_command("cancel task") is True

    def test_stop_task(self):
        assert _is_cancel_task_command("stop task") is True

    def test_score_not_matched(self):
        assert _is_cancel_task_command("/score") is False

    def test_cancel_email_not_matched(self):
        assert _is_cancel_task_command("cancel email") is False


# ── git_ops dispatch ───────────────────────────────────────────────────────────

class TestGitOpsDispatch:
    def test_dispatch_status(self):
        with patch("tools.git_ops._run", return_value="M router.py"):
            ok, out = dispatch("status", {})
        assert ok is True
        assert "router.py" in out

    def test_dispatch_diff(self):
        with patch("tools.git_ops._run", return_value="@@ -1 +1 @@\n+new line"):
            ok, out = dispatch("diff", {})
        assert ok is True
        assert "new line" in out

    def test_dispatch_log(self):
        with patch("tools.git_ops._run", return_value="abc1234 feat: add X"):
            ok, out = dispatch("log", {"n": 5})
        assert ok is True
        assert "feat: add X" in out

    def test_dispatch_commit(self):
        with patch("tools.git_ops._run", return_value="[main abc1234] my message"):
            ok, out = dispatch("commit", {"message": "my message"})
        assert ok is True
        assert "my message" in out

    def test_dispatch_add_all(self):
        with patch("tools.git_ops._run", return_value="M file.py"):
            ok, out = dispatch("add_all", {})
        assert ok is True

    def test_dispatch_unknown_action(self):
        ok, out = dispatch("push", {})
        assert ok is False
        assert "Unknown git action" in out

    def test_commit_rejects_short_message(self):
        ok_before, _ = True, None
        out = git_commit("ab")
        assert "too short" in out

    def test_commit_rejects_shell_injection(self):
        out = git_commit("message; rm -rf /")
        assert "disallowed" in out


# ── safe path ─────────────────────────────────────────────────────────────────

class TestSafePath:
    def test_rejects_bare_dot(self):
        with pytest.raises(ValueError, match="not allowed"):
            _safe_path(".")

    def test_rejects_dot_dot(self):
        with pytest.raises(ValueError, match="not allowed"):
            _safe_path("..")


# ── operative cancel_event ─────────────────────────────────────────────────────

class TestOperativeCancelEvent:
    def test_cancel_event_stops_before_next_step(self):
        """Steps after cancel_event.set() are skipped."""
        import operative
        from task_planner import TaskStep

        visited = []
        cancel = threading.Event()

        def _fake_plan(task):
            return [
                TaskStep(number=1, description="step1", tool="chat"),
                TaskStep(number=2, description="step2", tool="chat"),
                TaskStep(number=3, description="step3", tool="chat"),
            ]

        def _fake_execute(step, step_results, run_id=""):
            visited.append(step.number)
            if step.number == 1:
                cancel.set()
            return True, f"result_{step.number}"

        def _fake_summarize(prompt, system_extra=""):
            return "partial summary"

        with patch("operative.plan_task", side_effect=_fake_plan), \
             patch("operative.execute_step", side_effect=_fake_execute), \
             patch("operative._summarize", side_effect=_fake_summarize), \
             patch("operative.preflect.is_enabled", return_value=False):
            result = operative.run_task("do three things", cancel_event=cancel)

        assert 1 in visited
        assert 2 not in visited, "Step 2 should be skipped after cancel"
        assert 3 not in visited, "Step 3 should be skipped after cancel"

    def test_no_cancel_runs_all_steps(self):
        import operative
        from task_planner import TaskStep

        visited = []

        def _fake_plan(task):
            return [
                TaskStep(number=1, description="s1", tool="chat"),
                TaskStep(number=2, description="s2", tool="chat"),
            ]

        def _fake_execute(step, step_results, run_id=""):
            visited.append(step.number)
            return True, "ok"

        with patch("operative.plan_task", side_effect=_fake_plan), \
             patch("operative.execute_step", side_effect=_fake_execute), \
             patch("operative._summarize", return_value="done"), \
             patch("operative.preflect.is_enabled", return_value=False):
            result = operative.run_task("two steps")

        assert visited == [1, 2]


# ── git_ops live (read-only) ───────────────────────────────────────────────────

class TestGitOpsLive:
    """Read-only ops against the actual jarvis-ai repo — no writes."""

    def test_git_status_returns_string(self):
        out = git_status()
        assert isinstance(out, str)
        assert len(out) > 0

    def test_git_diff_returns_string(self):
        out = git_diff()
        assert isinstance(out, str)

    def test_git_log_returns_commits(self):
        out = git_log(n=3)
        assert isinstance(out, str)
        assert len(out) > 0

    def test_git_diff_staged_returns_string(self):
        out = git_diff(staged=True)
        assert isinstance(out, str)


# ── route_stream integration ───────────────────────────────────────────────────

class TestRouteStreamGitIntegration:
    def test_git_status_routed_to_git_label(self):
        with patch("tools.git_ops._run", return_value="M router.py"):
            from router import route_stream
            stream, label = route_stream("git status")
        assert label == "Git"
        out = "".join(stream)
        assert "router.py" in out

    def test_git_diff_routed(self):
        with patch("tools.git_ops._run", return_value="diff --git a/x b/x\n+new"):
            from router import route_stream
            stream, label = route_stream("show me the diff")
        assert label == "Git"

    def test_git_log_routed(self):
        with patch("tools.git_ops._run", return_value="abc1234 feat: shipped"):
            from router import route_stream
            stream, label = route_stream("show recent commits")
        assert label == "Git"

    def test_cancel_no_active_task(self):
        import router
        router._active_task_cancel = None
        from router import route_stream
        stream, label = route_stream("/cancel")
        assert label == "Operative"
        out = "".join(stream)
        assert "No active task" in out
