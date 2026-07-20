from __future__ import annotations

import multiprocessing
import time
from pathlib import Path

from harness.state_lock import queue_state_lock


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
