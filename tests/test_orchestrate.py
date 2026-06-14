"""Tests for orchestrate.py — the conductor layer over ADE.

The safety-critical surface is build_dispatch_env: an unattended dispatch must
always auto-approve the plan gate and scope (or skip) the test phase, or the
spawned agent hangs / runs the full repo suite on every lane.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import orchestrate


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


@pytest.fixture
def lane_repo(tmp_path: Path) -> Path:
    """A repo with a `main` base commit and an `ade/lane` branch that COMMITS a
    new file on top — the exact ADE shape: the lane's work lives in a commit, not
    the working tree."""
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", str(tmp_path)],
                   check=True, capture_output=True, text=True)
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "base.txt").write_text("base\n")
    _git(tmp_path, "add", "base.txt")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "checkout", "-b", "ade/lane")
    (tmp_path / "test_lane.py").write_text("\n".join(f"def test_{i}():\n    assert True" for i in range(6)) + "\n")
    _git(tmp_path, "add", "test_lane.py")
    _git(tmp_path, "commit", "-m", "lane work")
    return tmp_path


class TestBuildDispatchEnv:
    def test_always_auto_approves_plan(self):
        env = orchestrate.build_dispatch_env({}, tests=None, skip_verify=False, supervised=False)
        assert env["ADE_AUTO_APPROVE_PLAN"] == "1"

    def test_skip_permissions_on_by_default_for_unattended(self):
        env = orchestrate.build_dispatch_env({}, tests=None, skip_verify=False, supervised=False)
        assert env["ADE_CLAUDE_SKIP_PERMISSIONS"] == "1"

    def test_supervised_opts_out_of_skip_permissions(self):
        env = orchestrate.build_dispatch_env({}, tests=None, skip_verify=False, supervised=True)
        assert env.get("ADE_CLAUDE_SKIP_PERMISSIONS", "") != "1"

    def test_scoped_tests_set_test_cmd(self):
        env = orchestrate.build_dispatch_env(
            {}, tests="pytest tests/test_x.py -q", skip_verify=False, supervised=False
        )
        assert env["ADE_TEST_CMD"] == "pytest tests/test_x.py -q"
        assert "ADE_SKIP_VERIFY" not in env

    def test_skip_verify_sets_flag_and_omits_test_cmd(self):
        env = orchestrate.build_dispatch_env({}, tests=None, skip_verify=True, supervised=False)
        assert env["ADE_SKIP_VERIFY"] == "1"
        assert "ADE_TEST_CMD" not in env

    def test_no_scoping_means_full_suite_warning_is_caller_responsibility(self):
        # Neither tests nor skip_verify set: env carries no scoping keys, so the
        # loop falls back to detect_test_cmd (full suite). build does not inject one.
        env = orchestrate.build_dispatch_env({}, tests=None, skip_verify=False, supervised=False)
        assert "ADE_TEST_CMD" not in env
        assert "ADE_SKIP_VERIFY" not in env

    def test_preserves_base_environment(self):
        env = orchestrate.build_dispatch_env({"PATH": "/usr/bin", "HOME": "/h"}, tests=None, skip_verify=False, supervised=False)
        assert env["PATH"] == "/usr/bin"
        assert env["HOME"] == "/h"


class TestCollectStatus:
    def test_marks_done_lane_with_diff_as_ready_to_sync(self, tmp_path):
        state = {"lane-a": {"task_name": "lane-a", "status": "DONE", "retries": 0,
                            "worktree_path": str(tmp_path), "session": "ade-lane-a"}}
        with patch("orchestrate._load_state", return_value=state), \
             patch("orchestrate._live_sessions", return_value={"ade-lane-a"}), \
             patch("orchestrate._diff_stat", return_value=(3, 40)):
            rows = orchestrate.collect_status()
        row = rows[0]
        assert row["lane"] == "lane-a"
        assert row["status"] == "DONE"
        assert row["alive"] is True
        assert row["ready_to_sync"] is True

    def test_done_lane_with_empty_diff_is_not_ready(self, tmp_path):
        state = {"lane-b": {"task_name": "lane-b", "status": "DONE", "retries": 0,
                            "worktree_path": str(tmp_path), "session": "ade-lane-b"}}
        with patch("orchestrate._load_state", return_value=state), \
             patch("orchestrate._live_sessions", return_value=set()), \
             patch("orchestrate._diff_stat", return_value=(0, 0)):
            rows = orchestrate.collect_status()
        assert rows[0]["ready_to_sync"] is False
        assert rows[0]["alive"] is False

    def test_running_lane_is_not_ready_to_sync(self, tmp_path):
        state = {"lane-c": {"task_name": "lane-c", "status": "EXECUTING", "retries": 1,
                            "worktree_path": str(tmp_path), "session": "ade-lane-c"}}
        with patch("orchestrate._load_state", return_value=state), \
             patch("orchestrate._live_sessions", return_value={"ade-lane-c"}), \
             patch("orchestrate._diff_stat", return_value=(5, 99)):
            rows = orchestrate.collect_status()
        assert rows[0]["ready_to_sync"] is False


class TestDiffStatCountsCommittedWork:
    """Regression: ADE agents commit their output to the lane branch. The diff
    must be measured from the merge-base with trunk, not vs HEAD — otherwise a
    successful, committed lane reports 0 lines and looks like a failure."""

    def test_counts_committed_ahead_of_main(self, lane_repo):
        files, lines = orchestrate._diff_stat(lane_repo)
        # The lane committed test_lane.py (6 funcs / ~12 lines). vs-HEAD would be 0.
        assert files >= 1
        assert lines >= 6

    def test_counts_uncommitted_and_untracked_too(self, lane_repo):
        # Uncommitted edit to the committed file + a brand-new untracked file.
        (lane_repo / "test_lane.py").write_text("def test_extra():\n    assert True\n")
        (lane_repo / "PLAN.md").write_text("# plan\n")
        files, lines = orchestrate._diff_stat(lane_repo)
        assert files >= 2  # tracked change + untracked PLAN.md
        assert lines >= 1

    def test_falls_back_to_head_when_trunk_missing(self, tmp_path, monkeypatch):
        # No `main` branch resolvable → merge-base fails → fall back to HEAD diff,
        # which still surfaces uncommitted + untracked work (old behavior preserved).
        subprocess.run(["git", "-c", "init.defaultBranch=trunkless", "init", str(tmp_path)],
                       check=True, capture_output=True, text=True)
        _git(tmp_path, "config", "user.email", "t@example.com")
        _git(tmp_path, "config", "user.name", "Test")
        (tmp_path / "a.txt").write_text("a\n")
        _git(tmp_path, "add", "a.txt")
        _git(tmp_path, "commit", "-m", "c0")
        (tmp_path / "untracked.py").write_text("x = 1\n")
        files, lines = orchestrate._diff_stat(tmp_path)
        assert files >= 1  # untracked.py counted even with no `main`

    def test_empty_worktree_path_is_zero(self, tmp_path):
        assert orchestrate._diff_stat(tmp_path / "does-not-exist") == (0, 0)
