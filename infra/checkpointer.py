"""
infra/checkpointer.py — Session state checkpointing and teleportation.

Serializes running task state to disk and re-enqueues it on the event bus,
enabling tasks to survive session restarts, migrate between fleet workers,
or be resumed after partial failure.

Teleportation = checkpoint + re-enqueue. The task carries its own context,
progress markers, and retry count — any worker can pick up where it left off.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone as _tz

# Python 3.11+ exports datetime.UTC; use timezone.utc for 3.10 compatibility.
UTC = _tz.utc
from pathlib import Path
from typing import Any

logger = logging.getLogger("jarvis.infra.checkpointer")

_CHECKPOINT_DIR = Path(__file__).parent.parent / "runtime" / "checkpoints"


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class TaskCheckpoint:
    task_id: str
    agent: str
    title: str
    description: str
    context: dict[str, Any]
    progress: dict[str, Any]          # arbitrary agent-defined progress markers
    retry_count: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    source_worker: str = ""            # worker ID that wrote this checkpoint
    status: str = "checkpointed"      # checkpointed | resuming | completed | failed

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TaskCheckpoint":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ── Storage ───────────────────────────────────────────────────────────────────

def _ensure_dir() -> Path:
    _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    return _CHECKPOINT_DIR


def _checkpoint_path(task_id: str) -> Path:
    safe_id = task_id.replace("/", "_").replace("..", "")[:64]
    return _ensure_dir() / f"{safe_id}.json"


def save(checkpoint: TaskCheckpoint) -> bool:
    """Write checkpoint to disk. Returns True on success."""
    checkpoint.updated_at = datetime.now(UTC).isoformat()
    path = _checkpoint_path(checkpoint.task_id)
    try:
        path.write_text(json.dumps(checkpoint.to_dict(), indent=2), encoding="utf-8")
        logger.info("Checkpoint saved: %s → %s", checkpoint.task_id, path.name)
        return True
    except OSError as exc:
        logger.error("Failed to save checkpoint %s: %s", checkpoint.task_id, exc)
        return False


def load(task_id: str) -> TaskCheckpoint | None:
    """Load checkpoint from disk. Returns None if not found."""
    path = _checkpoint_path(task_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return TaskCheckpoint.from_dict(data)
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        logger.error("Failed to load checkpoint %s: %s", task_id, exc)
        return None


def delete(task_id: str) -> bool:
    """Remove checkpoint after successful completion."""
    path = _checkpoint_path(task_id)
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def list_checkpoints() -> list[TaskCheckpoint]:
    """Return all checkpoints currently on disk, sorted oldest-first."""
    checkpoints = []
    try:
        for p in sorted(_ensure_dir().glob("*.json"), key=lambda f: f.stat().st_mtime):
            cp = load(p.stem)
            if cp:
                checkpoints.append(cp)
    except OSError:
        pass
    return checkpoints


# ── Teleportation ─────────────────────────────────────────────────────────────

def teleport(checkpoint: TaskCheckpoint, target_worker: str | None = None) -> dict[str, Any]:
    """
    Serialize task state and re-enqueue it on the event bus.

    The task carries full context + progress markers, so any worker can resume
    from where it left off. Returns the event bus response or an error dict.

    Args:
        checkpoint: The task state to teleport.
        target_worker: Optional worker affinity hint (e.g., "worker-2").
                       Event bus may or may not honor it.
    """
    checkpoint.status = "resuming"
    checkpoint.retry_count += 1
    save(checkpoint)

    payload = {
        "title": f"[RESUME] {checkpoint.title}",
        "description": checkpoint.description,
        "agent": checkpoint.agent,
        "context": {
            **checkpoint.context,
            "_checkpoint_id": checkpoint.task_id,
            "_resume_progress": checkpoint.progress,
            "_retry_count": checkpoint.retry_count,
        },
    }
    if target_worker:
        payload["context"]["_target_worker"] = target_worker

    return _enqueue_on_event_bus(payload)


def _enqueue_on_event_bus(payload: dict) -> dict[str, Any]:
    """POST task to the local event bus /tasks endpoint."""
    try:
        import urllib.request
        import urllib.error

        event_bus_url = os.getenv("JARVIS_EVENT_BUS_URL", "http://127.0.0.1:8001")
        req_data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{event_bus_url}/tasks",
            data=req_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        logger.error("Failed to enqueue checkpoint on event bus: %s", exc)
        return {"ok": False, "error": str(exc)}


# ── Checkpoint manager for ADE ────────────────────────────────────────────────

class CheckpointManager:
    """
    Context manager that automatically checkpoints a task's progress.

    Usage:
        with CheckpointManager(task_id="t-001", agent="backend_engineer",
                               title="Add auth", description="...") as cp:
            cp.update_progress({"phase": "planning", "files_touched": []})
            # ... do work ...
            cp.update_progress({"phase": "executing", "files_touched": ["api.py"]})
        # On clean exit: checkpoint deleted.
        # On exception: checkpoint saved for teleportation.
    """

    def __init__(
        self,
        task_id: str,
        agent: str,
        title: str,
        description: str,
        context: dict[str, Any] | None = None,
    ):
        self.checkpoint = TaskCheckpoint(
            task_id=task_id,
            agent=agent,
            title=title,
            description=description,
            context=context or {},
            progress={},
        )

    def update_progress(self, progress: dict[str, Any]) -> None:
        self.checkpoint.progress.update(progress)
        save(self.checkpoint)

    def __enter__(self) -> "CheckpointManager":
        save(self.checkpoint)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_type is None:
            # Clean exit — task completed, remove checkpoint
            self.checkpoint.status = "completed"
            delete(self.checkpoint.task_id)
            logger.info("Task %s completed, checkpoint removed.", self.checkpoint.task_id)
        else:
            # Exception — preserve checkpoint for potential teleportation
            self.checkpoint.status = "failed"
            save(self.checkpoint)
            logger.warning(
                "Task %s failed (%s), checkpoint preserved for teleportation.",
                self.checkpoint.task_id,
                exc_type.__name__,
            )
        return False  # Do not suppress the exception
