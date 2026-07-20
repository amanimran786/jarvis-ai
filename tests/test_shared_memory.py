from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from harness import shared_memory


@pytest.fixture(autouse=True)
def _isolated_memory_dir(tmp_path, monkeypatch):
    # Arrange: point the module at a throwaway directory so tests never
    # touch the real vault on disk.
    monkeypatch.setattr(shared_memory, "SHARED_MEMORY_DIR", tmp_path / "memory" / "shared")
    yield


def test_write_then_read_round_trips():
    # Act
    shared_memory.write("session_focus", {"task": "cross-session-memory"})
    result = shared_memory.read("session_focus")

    # Assert
    assert result == {"task": "cross-session-memory"}


def test_read_missing_key_returns_default():
    # Act
    result = shared_memory.read("does_not_exist", default="fallback")

    # Assert
    assert result == "fallback"


def test_read_missing_key_returns_none_by_default():
    # Act / Assert
    assert shared_memory.read("does_not_exist") is None


def test_write_overwrites_existing_value():
    # Arrange
    shared_memory.write("counter", 1)

    # Act
    shared_memory.write("counter", 2)

    # Assert
    assert shared_memory.read("counter") == 2


def test_list_keys_returns_sorted_known_keys():
    # Arrange
    shared_memory.write("zeta", 1)
    shared_memory.write("alpha", 2)

    # Act
    keys = shared_memory.list_keys()

    # Assert
    assert keys == ["alpha", "zeta"]


def test_list_keys_empty_when_nothing_written():
    # Act / Assert
    assert shared_memory.list_keys() == []


@pytest.mark.parametrize("bad_key", ["", "../escape", "a/b", "a b", "..", "/abs"])
def test_invalid_key_rejected_on_write(bad_key):
    # Act / Assert
    with pytest.raises(shared_memory.SharedMemoryError):
        shared_memory.write(bad_key, "value")


@pytest.mark.parametrize("bad_key", ["", "../escape", "a/b", "a b"])
def test_invalid_key_rejected_on_read(bad_key):
    # Act / Assert
    with pytest.raises(shared_memory.SharedMemoryError):
        shared_memory.read(bad_key)


def test_write_leaves_no_leftover_tmp_files():
    # Act
    shared_memory.write("clean_write", "value")

    # Assert
    entries = list(shared_memory.SHARED_MEMORY_DIR.glob("*"))
    names = [p.name for p in entries]
    assert "clean_write.json" in names
    assert not any(name.endswith(".tmp") for name in names)


def test_corrupt_entry_raises_on_read():
    # Arrange
    shared_memory.SHARED_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    corrupt_path = shared_memory.SHARED_MEMORY_DIR / "broken.json"
    corrupt_path.write_text("{not valid json", encoding="utf-8")

    # Act / Assert
    with pytest.raises(shared_memory.SharedMemoryError):
        shared_memory.read("broken")


def _write_from_subprocess(shared_dir: str, key: str, value: str) -> None:
    from harness import shared_memory as sm

    sm.SHARED_MEMORY_DIR = Path(shared_dir)
    sm.write(key, value)


def test_concurrent_writes_from_separate_processes_do_not_corrupt(tmp_path):
    # Arrange
    shared_dir = tmp_path / "memory" / "shared"
    shared_memory.SHARED_MEMORY_DIR = shared_dir
    processes = [
        multiprocessing.Process(
            target=_write_from_subprocess,
            args=(str(shared_dir), f"key_{i}", f"value_{i}"),
        )
        for i in range(8)
    ]

    # Act
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)

    # Assert
    for i, process in enumerate(processes):
        assert process.exitcode == 0, f"writer {i} failed"
    for i in range(8):
        entry_path = shared_dir / f"key_{i}.json"
        payload = json.loads(entry_path.read_text(encoding="utf-8"))
        assert payload["value"] == f"value_{i}"
    assert shared_memory.list_keys() == sorted(f"key_{i}" for i in range(8))


def test_concurrent_writes_to_same_key_leave_one_consistent_winner(tmp_path):
    # Arrange
    shared_dir = tmp_path / "memory" / "shared"
    shared_memory.SHARED_MEMORY_DIR = shared_dir
    processes = [
        multiprocessing.Process(
            target=_write_from_subprocess,
            args=(str(shared_dir), "contested_key", f"writer_{i}"),
        )
        for i in range(6)
    ]

    # Act
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)

    # Assert: file is valid JSON (no interleaved/torn writes) and holds
    # exactly one of the writers' values.
    for process in processes:
        assert process.exitcode == 0
    entry_path = shared_dir / "contested_key.json"
    payload = json.loads(entry_path.read_text(encoding="utf-8"))
    assert payload["value"] in {f"writer_{i}" for i in range(6)}
