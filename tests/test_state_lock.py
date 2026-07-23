from __future__ import annotations

import multiprocessing
import os
import subprocess
import time
from pathlib import Path

import pytest

from harness.session_tracker import SessionTracker
from harness.state_lock import _lock_path, queue_state_lock, state_file_lock


def _acquire_and_mark(queue_path: str, marker_path: str) -> None:
    with queue_state_lock(Path(queue_path)):
        Path(marker_path).write_text("acquired", encoding="utf-8")


def test_queue_state_lock_serializes_processes(tmp_path: Path) -> None:
    queue_path = tmp_path / "WORK_QUEUE.json"
    marker_path = tmp_path / "acquired.txt"
    process = multiprocessing.Process(
        target=_acquire_and_mark,
        args=(str(queue_path), str(marker_path)),
    )

    with queue_state_lock(queue_path):
        process.start()
        time.sleep(0.2)
        assert not marker_path.exists()

    process.join(timeout=3)
    if process.is_alive():
        process.terminate()
        process.join(timeout=1)
        raise AssertionError("child process did not acquire released queue lock")
    assert process.exitcode == 0
    assert marker_path.read_text(encoding="utf-8") == "acquired"


def test_state_lock_rejects_symlink_without_chmod_target(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    external = tmp_path / "external.txt"
    external.write_text("preserve\n", encoding="utf-8")
    external.chmod(0o644)
    lock_path = _lock_path(state_path, "state")
    try:
        lock_path.unlink(missing_ok=True)
        lock_path.symlink_to(external)

        with pytest.raises(OSError):
            with state_file_lock(state_path):
                pass

        assert external.read_text(encoding="utf-8") == "preserve\n"
        assert external.stat().st_mode & 0o777 == 0o644
    finally:
        if lock_path.is_symlink():
            lock_path.unlink()


def test_session_lock_does_not_dirty_repository(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "init", "-q"],
        cwd=tmp_path,
        check=True,
        timeout=30,
        shell=False,
    )
    (tmp_path / ".gitignore").write_text(
        "ACTIVE_SESSIONS.json\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", ".gitignore"],
        cwd=tmp_path,
        check=True,
        timeout=30,
        shell=False,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=State Lock Test",
            "-c",
            "user.email=state-lock@example.com",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        cwd=tmp_path,
        check=True,
        timeout=30,
        shell=False,
    )
    tracker = SessionTracker(tmp_path / "ACTIVE_SESSIONS.json")

    tracker.claim("TASK-1", "session-1")

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    assert status.stdout == ""
