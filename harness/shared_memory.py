"""Cross-session shared memory backed by the local vault.

Any Jarvis session (or the orchestrator) can write a named entry here and
have every other session — in-repo or packaged — read the same value back,
since entries live under ``vault.VAULT_ROOT`` rather than per-process memory.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import vault

SHARED_MEMORY_DIR = vault.VAULT_ROOT / "memory" / "shared"

_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_LOCK_FILENAME = ".shared_memory.lock"


class SharedMemoryError(ValueError):
    """Raised for invalid keys or unreadable/corrupt entries."""


def _validate_key(key: str) -> str:
    if not isinstance(key, str) or not _KEY_PATTERN.match(key) or key in {".", ".."}:
        raise SharedMemoryError(f"invalid shared memory key: {key!r}")
    return key


def _entry_path(key: str) -> Path:
    _validate_key(key)
    path = (SHARED_MEMORY_DIR / f"{key}.json").resolve()
    if path.parent != SHARED_MEMORY_DIR.resolve():
        raise SharedMemoryError(f"invalid shared memory key: {key!r}")
    return path


@contextmanager
def _memory_lock() -> Iterator[None]:
    """Serialize read-modify-write cycles across processes and sessions."""
    SHARED_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = SHARED_MEMORY_DIR / _LOCK_FILENAME
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def write(key: str, value: Any) -> None:
    """Persist a named entry so any session reading the vault sees it."""
    path = _entry_path(key)
    payload = {
        "key": key,
        "value": value,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with _memory_lock():
        _atomic_write(path, payload)


def read(key: str, default: Any = None) -> Any:
    """Read back a named entry's value, or ``default`` if it isn't set."""
    path = _entry_path(key)
    with _memory_lock():
        if not path.exists():
            return default
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise SharedMemoryError(f"corrupt shared memory entry: {key!r}") from exc
    return payload.get("value", default)


def list_keys() -> list[str]:
    """List all known shared memory keys, sorted."""
    with _memory_lock():
        if not SHARED_MEMORY_DIR.exists():
            return []
        return sorted(p.stem for p in SHARED_MEMORY_DIR.glob("*.json"))
