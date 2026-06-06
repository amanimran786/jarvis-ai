"""
ade/state.py — Per-task state stored in <repo_root>/.worktrees/.ade_state.json
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_FILENAME = ".ade_state.json"

# Valid status values (in lifecycle order)
PLANNING   = "PLANNING"
EXECUTING  = "EXECUTING"
VERIFYING  = "VERIFYING"
RETRYING   = "RETRYING"
DONE       = "DONE"
FAILED     = "FAILED"


def _state_path(repo_root: Path) -> Path:
    return repo_root / ".worktrees" / _FILENAME


def load(repo_root: Path) -> dict[str, Any]:
    f = _state_path(repo_root)
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(repo_root: Path, data: dict[str, Any]) -> None:
    f = _state_path(repo_root)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=2))


def upsert(repo_root: Path, task_name: str, **fields: Any) -> None:
    data = load(repo_root)
    if task_name not in data:
        data[task_name] = {"task_name": task_name, "started_at": time.time()}
    data[task_name].update(fields)
    data[task_name]["updated_at"] = time.time()
    _save(repo_root, data)


def set_status(repo_root: Path, task_name: str, status: str, **extra: Any) -> None:
    upsert(repo_root, task_name, status=status, **extra)


def get(repo_root: Path, task_name: str) -> dict[str, Any] | None:
    return load(repo_root).get(task_name)


def remove(repo_root: Path, task_name: str) -> None:
    data = load(repo_root)
    data.pop(task_name, None)
    _save(repo_root, data)
