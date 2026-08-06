"""Cross-process locks for shared Jarvis JSON state."""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def _lock_path(state_path: Path, namespace: str) -> Path:
    temp_root = Path(
        os.environ.get("TMPDIR") or tempfile.gettempdir()
    ).resolve(strict=True)
    root = temp_root / f"jarvis-state-locks-{os.getuid()}"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = root.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or root.resolve(strict=True) != root
    ):
        raise OSError("Jarvis state lock directory is unsafe")
    root.chmod(0o700)
    identity = str(Path(state_path).expanduser().absolute()).encode(
        "utf-8",
        errors="surrogateescape",
    )
    digest = hashlib.sha256(identity).hexdigest()
    return root / f"{namespace}-{digest}.lock"


@contextmanager
def _state_lock(state_path: Path, namespace: str) -> Iterator[None]:
    lock_path = _lock_path(state_path, namespace)
    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise OSError("Jarvis state lock target is unsafe")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@contextmanager
def state_file_lock(state_path: Path) -> Iterator[None]:
    """Serialize read-modify-write cycles for one state file."""
    with _state_lock(Path(state_path), "state"):
        yield


@contextmanager
def queue_state_lock(queue_path: Path) -> Iterator[None]:
    """Serialize read-modify-write cycles for one work queue."""
    with _state_lock(Path(queue_path), "queue"):
        yield
