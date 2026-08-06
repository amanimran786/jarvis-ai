"""Regression tests for the orphaned .git/index.lock fix in
harness/agent_coordinator.py.

Root cause: _run_git() ran read-only queries — `git status --porcelain` and
`git rev-parse HEAD` — directly against the shared checkout via
_clean_repo_head(), which every `claim` and `finish` call makes on the live
repo before touching WORK_QUEUE.json (see claim_next()/finish()). The
subprocess had a hard `timeout=30`. `git status` can opportunistically
refresh and rewrite the on-disk index, which briefly takes .git/index.lock;
subprocess.run(timeout=...) kills the process with SIGKILL on expiry, so a
kill mid-refresh leaves that lock file orphaned — zero bytes, no owning
process — and every later git command in the shared checkout then fails with
"Unable to create '.git/index.lock': File exists." until a human deletes it.

Fix: pass --no-optional-locks on these calls so they never attempt to write
the index at all — there's nothing for a kill to interrupt.

These tests never touch the real /Users/truthseeker/jarvis-ai/.git — every
repo here is a throwaway one under tmp_path.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from harness.agent_coordinator import CoordinationError, _clean_repo_head, _run_git


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        shell=False,
        timeout=30,
    )
    return result.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Coordinator Test")
    (repo / "a.txt").write_text("hi\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "base")
    return repo


class TestNoOptionalLocksFlag:
    def test_run_git_passes_no_optional_locks(self, tmp_path):
        """_run_git is only ever used for read-only queries (status,
        rev-parse) — assert the flag that stops git from writing the index
        is actually present in the argv handed to subprocess.run."""
        repo = _init_repo(tmp_path)
        with patch("harness.agent_coordinator.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            _run_git(repo, "status", "--porcelain", "--untracked-files=all")

        argv = mock_run.call_args.args[0]
        assert argv[0] == "git"
        assert "--no-optional-locks" in argv
        assert argv.index("--no-optional-locks") < argv.index("status")

    def test_rev_parse_also_passes_no_optional_locks(self, tmp_path):
        repo = _init_repo(tmp_path)
        with patch("harness.agent_coordinator.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="deadbeef\n", stderr=""
            )
            _run_git(repo, "rev-parse", "HEAD")

        argv = mock_run.call_args.args[0]
        assert "--no-optional-locks" in argv

    def test_status_query_does_not_rewrite_index(self, tmp_path):
        """Behavioral proof with the real git binary, no mocking: a plain
        `git status` opportunistically rewrites .git/index (observable via
        its mtime) when there's a dirty tracked file to refresh. That write
        is exactly what a timeout-driven kill can interrupt and orphan.
        With --no-optional-locks the index must be left untouched."""
        repo = _init_repo(tmp_path)
        index_path = repo / ".git" / "index"
        (repo / "a.txt").write_text("hi\nmore\n")
        time.sleep(1.1)  # guarantee a distinguishable mtime tick
        before = index_path.stat().st_mtime
        _run_git(repo, "status", "--porcelain", "--untracked-files=all")
        after = index_path.stat().st_mtime
        assert after == before, (
            "status query rewrote the index — this is the exact write a "
            "killed/timed-out process would orphan as .git/index.lock"
        )


class TestTimeoutDoesNotOrphanState:
    def test_timeout_is_raised_not_swallowed(self, tmp_path):
        repo = _init_repo(tmp_path)
        with patch(
            "harness.agent_coordinator.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["git", "status"], timeout=30),
        ):
            with pytest.raises(CoordinationError):
                _run_git(repo, "status", "--porcelain", "--untracked-files=all")

    def test_timeout_leaves_repo_usable_for_the_next_call(self, tmp_path):
        """A timed-out/killed call must not leave .git/index.lock behind,
        and a subsequent real call against the same repo must still
        succeed — i.e. the failure is contained to the one call, not the
        whole repo."""
        repo = _init_repo(tmp_path)
        lock_path = repo / ".git" / "index.lock"

        with patch(
            "harness.agent_coordinator.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["git", "status"], timeout=30),
        ):
            with pytest.raises(CoordinationError):
                _run_git(repo, "status", "--porcelain", "--untracked-files=all")

        assert not lock_path.exists()

        # Real, unmocked call proves the repo is still fully usable.
        head = _run_git(repo, "rev-parse", "HEAD")
        assert len(head) == 40

    def test_clean_repo_head_succeeds_after_recovering_from_a_timeout(self, tmp_path):
        """claim_next()/finish() both call _clean_repo_head() first. A prior
        transient timeout on one call must not poison later calls in the
        same process."""
        repo = _init_repo(tmp_path)
        real_run = subprocess.run

        def _flaky_run(argv, **kwargs):
            if argv[:3] == ["git", "--no-optional-locks", "status"]:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=30)
            return real_run(argv, **kwargs)

        with patch("harness.agent_coordinator.subprocess.run", side_effect=_flaky_run):
            with pytest.raises(CoordinationError):
                _clean_repo_head(repo)

        # No mock this time — the flaky failure must not have left the repo
        # in a state the next, real call can't recover from.
        head = _clean_repo_head(repo)
        assert len(head) == 40
