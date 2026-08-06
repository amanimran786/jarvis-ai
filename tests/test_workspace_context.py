"""Tests for workspace_context.snapshot() and format_for_prompt()."""
from __future__ import annotations

import subprocess
import threading
import time
from unittest.mock import patch

import workspace_context


def _clear_cache():
    """Reset module-level cache between tests."""
    workspace_context._cache = None
    workspace_context._cache_ts = 0.0


class TestSnapshot:
    def setup_method(self):
        _clear_cache()

    def test_returns_required_keys(self):
        snap = workspace_context.snapshot()
        for key in ("git_status", "recent_commits", "directory_tree", "key_files", "root"):
            assert key in snap

    def test_git_status_is_string(self):
        snap = workspace_context.snapshot()
        assert isinstance(snap["git_status"], str)

    def test_key_files_are_list(self):
        snap = workspace_context.snapshot()
        assert isinstance(snap["key_files"], list)

    def test_cache_returns_same_object(self):
        snap1 = workspace_context.snapshot()
        snap2 = workspace_context.snapshot()
        assert snap1 is snap2

    def test_invalidate_clears_cache(self):
        snap1 = workspace_context.snapshot()
        workspace_context.invalidate()
        snap2 = workspace_context.snapshot()
        assert snap1 is not snap2

    def test_cache_expires_after_ttl(self):
        snap1 = workspace_context.snapshot()
        workspace_context._cache_ts = time.monotonic() - (workspace_context._CACHE_TTL + 1)
        snap2 = workspace_context.snapshot()
        assert snap1 is not snap2

    def test_git_failures_return_empty_string(self):
        _clear_cache()
        with patch("workspace_context._run", return_value=""):
            snap = workspace_context.snapshot()
        assert snap["git_status"] == ""
        assert snap["recent_commits"] == ""

    def test_git_status_uses_no_optional_locks(self):
        """_git_status() is a read-only query and must never take
        .git/index.lock. A plain `git status` can opportunistically refresh
        and rewrite the on-disk index (briefly taking the lock); if that
        subprocess is ever killed on timeout mid-write, the lock is
        orphaned and blocks every later git command in the repo.
        --no-optional-locks keeps this call a pure read.
        """
        with patch(
            "workspace_context.subprocess.check_output", return_value=""
        ) as mock_check_output:
            workspace_context._git_status()
        argv = mock_check_output.call_args.args[0]
        assert argv[0] == "git"
        assert "--no-optional-locks" in argv
        assert argv.index("--no-optional-locks") < argv.index("status")

    def test_run_logs_failures_instead_of_swallowing_silently(self):
        """_run() must not swallow subprocess failures with no trace at
        all — that hid the fact this code path was ever failing/timing
        out. A debug log line is enough since not-a-git-repo is a benign,
        expected failure mode for this helper."""
        with patch(
            "workspace_context.subprocess.check_output",
            side_effect=subprocess.TimeoutExpired(cmd=["git", "status"], timeout=8),
        ), patch("workspace_context.log.debug") as mock_debug:
            result = workspace_context._run(["git", "status"])
        assert result == ""
        mock_debug.assert_called_once()

    def test_key_files_only_existing(self, tmp_path, monkeypatch):
        """_key_files() must skip files that don't exist in the root."""
        monkeypatch.setattr(workspace_context, "_ROOT", tmp_path)
        _clear_cache()
        # Create only config.py
        (tmp_path / "config.py").write_text("x=1")
        snap = workspace_context.snapshot()
        assert "config.py" in snap["key_files"]
        assert "router.py" not in snap["key_files"]

    def test_thread_safety(self):
        """Concurrent snapshot() calls must not corrupt the cache."""
        results = []

        def _call():
            results.append(workspace_context.snapshot())

        # Keep this a concurrency test rather than an integration test over the
        # real repository.  Walking a busy worktree from worker threads makes
        # the suite depend on local filesystem size and can take minutes.
        with patch("workspace_context._run", return_value=""), patch(
            "workspace_context._dir_tree", return_value=""
        ):
            threads = [threading.Thread(target=_call) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=2)

        assert all(not t.is_alive() for t in threads)

        assert len(results) == 8
        # All threads see the same cached object (same id)
        ids = {id(r) for r in results}
        assert len(ids) == 1


class TestFormatForPrompt:
    def setup_method(self):
        _clear_cache()

    def test_includes_workspace_header(self):
        text = workspace_context.format_for_prompt()
        assert "## Workspace:" in text

    def test_accepts_pre_built_snapshot(self):
        snap = {
            "root": "/tmp/test",
            "git_status": "M foo.py",
            "recent_commits": "abc1234 fix bug",
            "key_files": ["config.py"],
            "directory_tree": "",
        }
        text = workspace_context.format_for_prompt(snap)
        assert "M foo.py" in text
        assert "abc1234" in text
        assert "config.py" in text

    def test_skips_empty_sections(self):
        snap = {
            "root": "/tmp/test",
            "git_status": "",
            "recent_commits": "",
            "key_files": [],
            "directory_tree": "",
        }
        text = workspace_context.format_for_prompt(snap)
        assert "Git status" not in text
        assert "Recent commits" not in text
