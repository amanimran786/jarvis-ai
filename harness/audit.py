"""
harness/audit.py — Structured audit logging for Jarvis.

Every significant Jarvis action is appended to logs/audit.jsonl as a
JSON-lines record.  The module is stdlib-only so it can be imported from
anywhere without circular-import risk.

Public API:
    audit_log(event_type, **kwargs)  — append one audit record
    session_id()                     — the current process session UUID
    start_session(name)              — log session_start + snapshot memory
    end_session(*, crashed=False)    — log session_end + write ops ledger entry
    list_snapshots()                 — list available memory snapshots
    restore_snapshot(snapshot_dir)   — restore memory from a snapshot dir
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# ── Paths ─────────────────────────────────────────────────────────────────────

def _base_dir() -> Path:
    """Project root (dev) or ~/Library/Application Support/Jarvis (packaged)."""
    try:
        import sys
        if getattr(sys, "frozen", False):
            import runtime_state
            return runtime_state.app_data_dir()
    except Exception:
        pass
    return Path(__file__).resolve().parent.parent


_LOGS_DIR: Path | None = None
_SNAPSHOTS_DIR: Path | None = None
_ORCH_STATUS_PATH: Path | None = None


def _logs_dir() -> Path:
    global _LOGS_DIR
    if _LOGS_DIR is None:
        _LOGS_DIR = _base_dir() / "logs"
        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        (_LOGS_DIR / "archive").mkdir(exist_ok=True)
    return _LOGS_DIR


def _snapshots_dir() -> Path:
    global _SNAPSHOTS_DIR
    if _SNAPSHOTS_DIR is None:
        _SNAPSHOTS_DIR = _base_dir() / "snapshots"
        _SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    return _SNAPSHOTS_DIR


def _orch_status_path() -> Path:
    global _ORCH_STATUS_PATH
    if _ORCH_STATUS_PATH is None:
        _ORCH_STATUS_PATH = _base_dir() / "ORCHESTRATOR_STATUS.json"
    return _ORCH_STATUS_PATH


def _audit_log_path() -> Path:
    return _logs_dir() / "audit.jsonl"


def _ops_log_path() -> Path:
    return _logs_dir() / "operations.md"


# ── Session state ──────────────────────────────────────────────────────────────

_SESSION_ID = str(uuid.uuid4())
_SESSION_NAME = "jarvis"
_SESSION_START_TIME: datetime | None = None
_SESSION_QUERY_COUNT = 0
_SESSION_MEMORY_WRITES = 0
_SESSION_ERROR_COUNT = 0
_SESSION_SNAPSHOT_DIR: Path | None = None
_lock = threading.Lock()


def session_id() -> str:
    return _SESSION_ID


# ── Log rotation ───────────────────────────────────────────────────────────────

_ROTATION_DAYS = 30
_ROTATION_CHECKED = False


def _rotate_logs() -> None:
    global _ROTATION_CHECKED
    if _ROTATION_CHECKED:
        return
    _ROTATION_CHECKED = True
    try:
        archive = _logs_dir() / "archive"
        cutoff = datetime.now(timezone.utc) - timedelta(days=_ROTATION_DAYS)
        audit_path = _audit_log_path()
        if not audit_path.exists():
            return
        # Read first line to check timestamp of oldest entry
        with open(audit_path) as f:
            first_line = f.readline().strip()
        if not first_line:
            return
        try:
            first_record = json.loads(first_line)
            oldest_ts = datetime.fromisoformat(first_record["ts"])
            if oldest_ts.tzinfo is None:
                oldest_ts = oldest_ts.replace(tzinfo=timezone.utc)
        except Exception:
            return
        if oldest_ts < cutoff:
            stamp = oldest_ts.strftime("%Y-%m-%d")
            archive_target = archive / f"audit_{stamp}.jsonl"
            # Partition: lines older than cutoff go to archive, rest stay
            keep_lines = []
            archive_lines = []
            with open(audit_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        ts = datetime.fromisoformat(rec["ts"])
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts < cutoff:
                            archive_lines.append(line)
                        else:
                            keep_lines.append(line)
                    except Exception:
                        keep_lines.append(line)
            if archive_lines:
                with open(archive_target, "a") as f:
                    f.write("\n".join(archive_lines) + "\n")
            with open(audit_path, "w") as f:
                f.write("\n".join(keep_lines) + "\n" if keep_lines else "")
    except Exception:
        pass  # rotation failure must never crash the process


# ── Core write ────────────────────────────────────────────────────────────────

def audit_log(event_type: str, **kwargs: Any) -> None:
    """Append one audit record to logs/audit.jsonl (thread-safe)."""
    global _SESSION_QUERY_COUNT, _SESSION_MEMORY_WRITES, _SESSION_ERROR_COUNT
    _rotate_logs()

    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": _SESSION_ID,
        "event_type": event_type,
    }
    # Pull well-known fields to the top level; rest go into payload
    top_level = {"model_used", "tokens_in", "tokens_out", "success", "error"}
    payload = {}
    for k, v in kwargs.items():
        if k in top_level:
            record[k] = v
        else:
            payload[k] = v
    if payload:
        record["payload"] = payload

    if "success" not in record:
        record["success"] = record.get("error") is None

    # Maintain counters for the ops ledger
    if event_type == "query_received":
        _SESSION_QUERY_COUNT += 1
    elif event_type in ("memory_write", "memory_promotion"):
        _SESSION_MEMORY_WRITES += 1
    elif not record.get("success"):
        _SESSION_ERROR_COUNT += 1

    line = json.dumps(record, default=str)
    with _lock:
        try:
            with open(_audit_log_path(), "a") as f:
                f.write(line + "\n")
        except OSError:
            pass  # disk full or permissions — never crash the process

    # Keep ORCHESTRATOR_STATUS in sync
    if event_type in ("query_received", "response_sent", "session_start", "session_end"):
        _update_orch_status(event_type, payload)


# ── ORCHESTRATOR_STATUS.json ───────────────────────────────────────────────────

def _update_orch_status(event_type: str, payload: dict) -> None:
    try:
        path = _orch_status_path()
        try:
            with open(path) as f:
                doc = json.load(f)
        except Exception:
            doc = {}

        now_iso = datetime.now(timezone.utc).isoformat()
        sessions = doc.get("sessions") or []

        # Find or create entry for this session
        entry = next((s for s in sessions if s.get("session_id") == _SESSION_ID), None)
        if entry is None:
            entry = {
                "session_id": _SESSION_ID,
                "name": _SESSION_NAME,
                "last_active": now_iso,
                "current_task": None,
                "completed_tasks": [],
                "next_task": None,
                "status": "active",
            }
            sessions.append(entry)

        entry["last_active"] = now_iso

        if event_type == "session_start":
            entry["status"] = "active"
        elif event_type == "query_received":
            entry["current_task"] = payload.get("query", "")
            entry["status"] = "active"
        elif event_type == "response_sent":
            task = payload.get("query") or entry.get("current_task")
            if task:
                ct = entry.get("completed_tasks") or []
                ct.append({"task": task, "completed_at": now_iso})
                entry["completed_tasks"] = ct[-50:]
            entry["current_task"] = None
            entry["next_task"] = None
            entry["status"] = "idle"
        elif event_type == "session_end":
            entry["current_task"] = None
            entry["status"] = "offline"

        doc["sessions"] = sessions

        with _lock:
            tmp = str(path) + ".tmp"
            with open(tmp, "w") as f:
                json.dump(doc, f, indent=2)
            os.replace(tmp, path)
    except Exception:
        pass


# ── Session snapshots ──────────────────────────────────────────────────────────

_MAX_SNAPSHOTS = 10


def _snapshot_dir_for_now() -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return _snapshots_dir() / stamp


def _take_snapshot() -> Path | None:
    """Snapshot memory.json + memory/ dir into snapshots/<timestamp>/."""
    global _SESSION_SNAPSHOT_DIR
    try:
        base = _base_dir()
        snap = _snapshot_dir_for_now()
        snap.mkdir(parents=True, exist_ok=True)

        # memory.json
        mem_json = base / "memory.json"
        if mem_json.exists():
            shutil.copy2(mem_json, snap / "memory.json")

        # memory/ directory (semantic + episodic JSON files only)
        mem_dir = base / "memory"
        if mem_dir.is_dir():
            dest = snap / "memory"
            dest.mkdir(exist_ok=True)
            for json_file in mem_dir.rglob("*.json"):
                rel = json_file.relative_to(mem_dir)
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(json_file, target)

        # Write snapshot metadata
        meta = {
            "session_id": _SESSION_ID,
            "session_name": _SESSION_NAME,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "in_progress",
        }
        with open(snap / "snapshot_meta.json", "w") as f:
            json.dump(meta, f, indent=2)

        _SESSION_SNAPSHOT_DIR = snap
        _purge_old_snapshots()
        return snap
    except Exception:
        return None


def _mark_snapshot(status: str) -> None:
    if _SESSION_SNAPSHOT_DIR is None or not _SESSION_SNAPSHOT_DIR.exists():
        return
    try:
        meta_path = _SESSION_SNAPSHOT_DIR / "snapshot_meta.json"
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except Exception:
            meta = {}
        meta["status"] = status
        meta["marked_at"] = datetime.now(timezone.utc).isoformat()
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)
    except Exception:
        pass


def _purge_old_snapshots() -> None:
    try:
        snaps = sorted(_snapshots_dir().iterdir(), key=lambda p: p.name)
        excess = snaps[: max(0, len(snaps) - _MAX_SNAPSHOTS)]
        for old in excess:
            if old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
    except Exception:
        pass


def list_snapshots() -> list[dict]:
    """Return metadata for all available snapshots, newest first."""
    result = []
    try:
        for snap in sorted(_snapshots_dir().iterdir(), reverse=True):
            if not snap.is_dir():
                continue
            meta_path = snap / "snapshot_meta.json"
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
            except Exception:
                meta = {}
            result.append({
                "path": str(snap),
                "name": snap.name,
                "session_id": meta.get("session_id", "unknown"),
                "session_name": meta.get("session_name", "unknown"),
                "created_at": meta.get("created_at", snap.name),
                "status": meta.get("status", "unknown"),
            })
    except Exception:
        pass
    return result


def restore_snapshot(snapshot_path: str | Path) -> tuple[bool, str]:
    """
    Restore memory.json and memory/ from the given snapshot directory.
    Returns (success, message).
    """
    snap = Path(snapshot_path)
    if not snap.is_dir():
        return False, f"Snapshot directory not found: {snap}"

    try:
        base = _base_dir()

        mem_snap = snap / "memory.json"
        if mem_snap.exists():
            shutil.copy2(mem_snap, base / "memory.json")

        mem_dir_snap = snap / "memory"
        if mem_dir_snap.is_dir():
            dest = base / "memory"
            for json_file in mem_dir_snap.rglob("*.json"):
                rel = json_file.relative_to(mem_dir_snap)
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(json_file, target)

        meta_path = snap / "snapshot_meta.json"
        created_at = snap.name
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            created_at = meta.get("created_at", snap.name)
        except Exception:
            pass

        audit_log("memory_restore", snapshot=str(snap), created_at=created_at)
        return True, f"Restored memory from snapshot {snap.name} (created {created_at})"
    except Exception as exc:
        return False, f"Restore failed: {exc}"


# ── Operations ledger ──────────────────────────────────────────────────────────

def _append_ops_ledger(duration_secs: float, crashed: bool) -> None:
    try:
        now = datetime.now()
        status = "CRASHED" if crashed else "completed"
        mins, secs = divmod(int(duration_secs), 60)
        duration_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        entry = (
            f"\n## Session {now.strftime('%Y-%m-%d %H:%M:%S')} "
            f"(id: {_SESSION_ID[:8]}, name: {_SESSION_NAME})\n"
            f"- Status: {status}\n"
            f"- Duration: {duration_str}\n"
            f"- Queries handled: {_SESSION_QUERY_COUNT}\n"
            f"- Memory writes: {_SESSION_MEMORY_WRITES}\n"
            f"- Errors: {_SESSION_ERROR_COUNT}\n"
        )
        with _lock:
            with open(_ops_log_path(), "a") as f:
                f.write(entry)
    except Exception:
        pass


# ── Session lifecycle ──────────────────────────────────────────────────────────

def start_session(name: str = "jarvis") -> None:
    """Call once at process start. Logs session_start and snapshots memory."""
    global _SESSION_NAME, _SESSION_START_TIME
    _SESSION_NAME = name
    _SESSION_START_TIME = datetime.now(timezone.utc)

    snap = _take_snapshot()
    audit_log(
        "session_start",
        session_name=name,
        snapshot=str(snap) if snap else None,
    )

    # Register crash handler — atexit fires even on unhandled exceptions
    atexit.register(_on_exit_crash_guard)


_session_ended = False


def end_session() -> None:
    """Call on clean shutdown."""
    global _session_ended
    if _session_ended:
        return
    _session_ended = True
    _mark_snapshot("completed")
    duration = (
        (datetime.now(timezone.utc) - _SESSION_START_TIME).total_seconds()
        if _SESSION_START_TIME else 0.0
    )
    audit_log("session_end", duration_secs=duration, clean_exit=True)
    _append_ops_ledger(duration, crashed=False)


def _on_exit_crash_guard() -> None:
    global _session_ended
    if _session_ended:
        return
    _session_ended = True
    _mark_snapshot("interrupted")
    duration = (
        (datetime.now(timezone.utc) - _SESSION_START_TIME).total_seconds()
        if _SESSION_START_TIME else 0.0
    )
    audit_log("session_end", duration_secs=duration, clean_exit=False)
    _append_ops_ledger(duration, crashed=True)
