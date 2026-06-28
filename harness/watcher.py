"""
harness/watcher.py — File/folder change watcher using watchdog.

Public API:
    watch(path, callback=None) -> str   Start watching; fires callback(path) on change.
    unwatch(path) -> str                Stop watching a specific path.
    unwatch_all() -> int                Stop all active watches.
    list_watches() -> list[str]         Return currently watched paths.

Default callback sends a macOS notification via harness.notify.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

log = logging.getLogger(__name__)

# path (normalized str) → Observer
_watches: dict[str, Observer] = {}
_lock = threading.Lock()


# ── Event handler ─────────────────────────────────────────────────────────────

class _Handler(FileSystemEventHandler):
    """Fires callback when the watched path (file or any path in a dir) changes."""

    def __init__(self, watched_path: str, callback: Callable[[str], None]) -> None:
        super().__init__()
        self._watched = watched_path
        self._is_file = os.path.isfile(watched_path)
        self._callback = callback

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        src = str(event.src_path)
        if self._is_file and src != self._watched:
            return
        try:
            self._callback(src)
        except Exception:
            log.debug("[watcher] callback raised for %s", src, exc_info=True)

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        src = str(event.src_path)
        if self._is_file and src != self._watched:
            return
        try:
            self._callback(src)
        except Exception:
            log.debug("[watcher] callback raised for %s", src, exc_info=True)

    on_moved = on_created  # treat renames as creation of the destination


# ── Default callback ──────────────────────────────────────────────────────────

def _default_callback(changed_path: str) -> None:
    from harness.notify import notify
    name = os.path.basename(changed_path)
    notify("Jarvis — File Changed", f"{name} was modified")


# ── Public API ────────────────────────────────────────────────────────────────

def watch(path: str, callback: Callable[[str], None] | None = None) -> str:
    """Start watching `path` for changes. Returns a status string."""
    resolved = str(Path(os.path.expanduser(path)).resolve())

    if not os.path.exists(resolved):
        return f"Path not found: {resolved}"

    with _lock:
        if resolved in _watches:
            return f"Already watching: {resolved}"

    cb = callback or _default_callback
    watch_dir = resolved if os.path.isdir(resolved) else str(Path(resolved).parent)

    handler = _Handler(resolved, cb)
    observer = Observer()
    observer.schedule(handler, watch_dir, recursive=False)
    observer.start()

    with _lock:
        _watches[resolved] = observer

    label = "folder" if os.path.isdir(resolved) else "file"
    log.info("[watcher] watching %s: %s", label, resolved)
    return f"Now watching {label}: {resolved}"


def unwatch(path: str) -> str:
    """Stop watching a specific path. Returns a status string."""
    resolved = str(Path(os.path.expanduser(path)).resolve())

    with _lock:
        observer = _watches.pop(resolved, None)

    if observer is None:
        # try basename match as a convenience
        with _lock:
            for k, obs in list(_watches.items()):
                if os.path.basename(k) == os.path.basename(resolved):
                    _watches.pop(k)
                    observer = obs
                    resolved = k
                    break

    if observer is None:
        return f"Not watching: {path}"

    observer.stop()
    observer.join(timeout=2)
    log.info("[watcher] stopped watching: %s", resolved)
    return f"Stopped watching: {resolved}"


def unwatch_all() -> int:
    """Stop all active watches. Returns count stopped."""
    with _lock:
        items = list(_watches.items())
        _watches.clear()

    for _, obs in items:
        obs.stop()
    for _, obs in items:
        obs.join(timeout=2)

    log.info("[watcher] stopped %d watch(es)", len(items))
    return len(items)


def list_watches() -> list[str]:
    """Return currently watched paths."""
    with _lock:
        return list(_watches.keys())
