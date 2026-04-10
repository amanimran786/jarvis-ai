from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any


log = logging.getLogger("jarvis.task_persistence")

_LOCK = threading.RLock()
_INITIALIZED = False

_NON_TERMINAL_STATUSES = {"queued", "assigned", "running", "streaming"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def db_path() -> Path:
    override = os.getenv("JARVIS_TASK_DB_PATH", "").strip()
    if override:
        path = Path(override).expanduser()
    else:
        path = _repo_root() / "runtime" / "jarvis_tasks.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_schema() -> bool:
    global _INITIALIZED
    if _INITIALIZED:
        return True
    with _LOCK:
        if _INITIALIZED:
            return True
        try:
            with _connect() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS tasks (
                        id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        finished_at TEXT NOT NULL DEFAULT '',
                        payload_json TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_tasks_created_at
                    ON tasks(created_at DESC);

                    CREATE INDEX IF NOT EXISTS idx_tasks_status
                    ON tasks(status);

                    CREATE TABLE IF NOT EXISTS task_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT NOT NULL,
                        event_index INTEGER NOT NULL,
                        ts TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        FOREIGN KEY(task_id) REFERENCES tasks(id)
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS idx_task_events_task_event_index
                    ON task_events(task_id, event_index);

                    CREATE INDEX IF NOT EXISTS idx_task_events_task_id
                    ON task_events(task_id, id);

                    CREATE TABLE IF NOT EXISTS webhook_receipts (
                        source TEXT NOT NULL,
                        delivery_id TEXT NOT NULL,
                        received_at TEXT NOT NULL,
                        event_name TEXT NOT NULL DEFAULT '',
                        body_sha256 TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY(source, delivery_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_webhook_receipts_received_at
                    ON webhook_receipts(received_at);
                    """
                )
            _INITIALIZED = True
            return True
        except Exception:
            log.exception("task persistence schema init failed")
            return False


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def upsert_task(task: dict[str, Any]) -> bool:
    if not _ensure_schema():
        return False
    try:
        payload = {
            "id": str(task.get("id") or ""),
            "status": str(task.get("status") or ""),
            "created_at": str(task.get("created_at") or ""),
            "updated_at": str(task.get("updated_at") or task.get("created_at") or ""),
            "finished_at": str(task.get("finished_at") or ""),
            "payload_json": _json_dumps(task),
        }
        if not payload["id"]:
            return False
        with _LOCK:
            with _connect() as conn:
                conn.execute(
                    """
                    INSERT INTO tasks (id, status, created_at, updated_at, finished_at, payload_json)
                    VALUES (:id, :status, :created_at, :updated_at, :finished_at, :payload_json)
                    ON CONFLICT(id) DO UPDATE SET
                        status=excluded.status,
                        created_at=excluded.created_at,
                        updated_at=excluded.updated_at,
                        finished_at=excluded.finished_at,
                        payload_json=excluded.payload_json
                    """,
                    payload,
                )
        return True
    except Exception:
        log.exception("task persistence upsert failed for task %s", task.get("id"))
        return False


def append_event(event: dict[str, Any], event_index: int) -> bool:
    if not _ensure_schema():
        return False
    try:
        task_id = str(event.get("task_id") or "")
        if not task_id:
            return False
        payload = {
            "task_id": task_id,
            "event_index": int(event_index),
            "ts": str(event.get("ts") or ""),
            "event_type": str(event.get("type") or ""),
            "payload_json": _json_dumps(event),
        }
        with _LOCK:
            with _connect() as conn:
                conn.execute(
                    """
                    INSERT INTO task_events (task_id, event_index, ts, event_type, payload_json)
                    VALUES (:task_id, :event_index, :ts, :event_type, :payload_json)
                    ON CONFLICT(task_id, event_index) DO UPDATE SET
                        ts=excluded.ts,
                        event_type=excluded.event_type,
                        payload_json=excluded.payload_json
                    """,
                    payload,
                )
        return True
    except Exception:
        log.exception("task persistence append event failed for task %s", event.get("task_id"))
        return False


def load_snapshot(limit: int = 250) -> dict[str, Any]:
    if not _ensure_schema():
        return {"ok": False, "tasks": [], "events": {}}
    try:
        with _LOCK:
            with _connect() as conn:
                rows = conn.execute(
                    """
                    SELECT payload_json
                    FROM tasks
                    WHERE status IN ({non_terminal}) OR id IN (
                        SELECT id
                        FROM tasks
                        ORDER BY created_at DESC
                        LIMIT ?
                    )
                    ORDER BY created_at DESC
                    """.format(non_terminal=",".join("?" for _ in _NON_TERMINAL_STATUSES)),
                    [*_NON_TERMINAL_STATUSES, max(int(limit), 1)],
                ).fetchall()
                tasks: list[dict[str, Any]] = []
                task_ids: list[str] = []
                seen: set[str] = set()
                for row in rows:
                    task = json.loads(row["payload_json"])
                    task_id = str(task.get("id") or "")
                    if not task_id or task_id in seen:
                        continue
                    seen.add(task_id)
                    task_ids.append(task_id)
                    tasks.append(task)

                events_by_task: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in task_ids}
                if task_ids:
                    placeholders = ",".join("?" for _ in task_ids)
                    event_rows = conn.execute(
                        f"""
                        SELECT task_id, payload_json
                        FROM task_events
                        WHERE task_id IN ({placeholders})
                        ORDER BY task_id ASC, event_index ASC, id ASC
                        """,
                        task_ids,
                    ).fetchall()
                    for row in event_rows:
                        events_by_task.setdefault(row["task_id"], []).append(json.loads(row["payload_json"]))
        return {"ok": True, "tasks": tasks, "events": events_by_task}
    except Exception:
        log.exception("task persistence bootstrap load failed")
        return {"ok": False, "tasks": [], "events": {}}


def register_webhook_receipt(
    source: str,
    delivery_id: str,
    event_name: str = "",
    body_sha256: str = "",
) -> bool:
    if not str(source or "").strip() or not str(delivery_id or "").strip():
        return True
    if not _ensure_schema():
        return True
    try:
        payload = {
            "source": str(source).strip(),
            "delivery_id": str(delivery_id).strip(),
            "event_name": str(event_name or ""),
            "body_sha256": str(body_sha256 or ""),
        }
        with _LOCK:
            with _connect() as conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO webhook_receipts (
                        source, delivery_id, received_at, event_name, body_sha256
                    )
                    VALUES (
                        :source,
                        :delivery_id,
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                        :event_name,
                        :body_sha256
                    )
                    """,
                    payload,
                )
                return cur.rowcount > 0
    except Exception:
        log.exception(
            "task persistence webhook receipt registration failed for source=%s delivery_id=%s",
            source,
            delivery_id,
        )
        return True


def prune_webhook_receipts(older_than_days: int = 30) -> int:
    if not _ensure_schema():
        return 0
    try:
        days = max(int(older_than_days), 1)
        with _LOCK:
            with _connect() as conn:
                cur = conn.execute(
                    """
                    DELETE FROM webhook_receipts
                    WHERE datetime(received_at) < datetime('now', ?)
                    """,
                    (f"-{days} days",),
                )
                return max(int(cur.rowcount or 0), 0)
    except Exception:
        log.exception("task persistence webhook receipt prune failed")
        return 0


def reset_for_tests() -> None:
    global _INITIALIZED
    with _LOCK:
        try:
            path = db_path()
            if path.exists():
                path.unlink()
            wal = path.with_suffix(path.suffix + "-wal")
            shm = path.with_suffix(path.suffix + "-shm")
            if wal.exists():
                wal.unlink()
            if shm.exists():
                shm.unlink()
        except Exception:
            log.exception("task persistence reset failed")
        finally:
            _INITIALIZED = False
