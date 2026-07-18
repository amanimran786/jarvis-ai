from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Mapping

import runtime_state

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
        path = runtime_state.writable_data_path(
            "runtime",
            "jarvis_tasks.sqlite3",
            seed_from=_repo_root() / "runtime" / "jarvis_tasks.sqlite3",
        )
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not override or not parent_existed:
        path.parent.chmod(0o700)
    return path


def _connect() -> sqlite3.Connection:
    path = db_path()
    conn = sqlite3.connect(str(path), timeout=5.0)
    path.chmod(0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            sidecar.chmod(0o600)
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

                    CREATE TABLE IF NOT EXISTS operative_approvals (
                        approval_id TEXT PRIMARY KEY,
                        manifest_digest TEXT NOT NULL,
                        principal TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        source TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        approved_at TEXT NOT NULL DEFAULT '',
                        grant_expires_at TEXT NOT NULL DEFAULT '',
                        consumed_at TEXT NOT NULL DEFAULT '',
                        cancelled_at TEXT NOT NULL DEFAULT '',
                        completed_at TEXT NOT NULL DEFAULT '',
                        outcome TEXT NOT NULL DEFAULT '',
                        run_id TEXT NOT NULL DEFAULT '',
                        manifest_json TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_operative_approvals_status_expiry
                    ON operative_approvals(status, expires_at);

                    CREATE TABLE IF NOT EXISTS operative_approval_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        approval_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        ts TEXT NOT NULL,
                        principal TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        source TEXT NOT NULL,
                        manifest_digest TEXT NOT NULL,
                        run_id TEXT NOT NULL DEFAULT ''
                    );

                    CREATE INDEX IF NOT EXISTS idx_operative_approval_events_id
                    ON operative_approval_events(approval_id, id);
                    """
                )
                approval_columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(operative_approvals)")
                }
                for column in ("completed_at", "outcome"):
                    if column not in approval_columns:
                        conn.execute(
                            f"ALTER TABLE operative_approvals "
                            f"ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                        )
            _INITIALIZED = True
            return True
        except Exception:
            log.exception("task persistence schema init failed")
            return False


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _task_row_payload(task: Mapping[str, Any]) -> dict[str, str]:
    return {
        "id": str(task.get("id") or ""),
        "status": str(task.get("status") or ""),
        "created_at": str(task.get("created_at") or ""),
        "updated_at": str(task.get("updated_at") or task.get("created_at") or ""),
        "finished_at": str(task.get("finished_at") or ""),
        "payload_json": _json_dumps(dict(task)),
    }


def _approval_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    record = dict(row)
    try:
        record["manifest"] = json.loads(record.pop("manifest_json"))
    except (KeyError, TypeError, json.JSONDecodeError):
        log.error("operative approval record contains invalid manifest JSON")
        return None
    return record


def _append_approval_event(
    conn: sqlite3.Connection,
    record: Mapping[str, Any],
    event_type: str,
    now_iso: str,
) -> None:
    conn.execute(
        """
        INSERT INTO operative_approval_events (
            approval_id, event_type, ts, principal, session_id, source,
            manifest_digest, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(record.get("approval_id") or ""),
            event_type,
            now_iso,
            str(record.get("principal") or ""),
            str(record.get("session_id") or ""),
            str(record.get("source") or ""),
            str(record.get("manifest_digest") or ""),
            str(record.get("run_id") or ""),
        ),
    )


def create_operative_proposal(record: Mapping[str, Any]) -> bool:
    """Persist one immutable pending execution proposal."""
    if not _ensure_schema():
        return False
    required = (
        "approval_id",
        "manifest_digest",
        "principal",
        "session_id",
        "source",
        "created_at",
        "expires_at",
        "manifest",
    )
    if any(not record.get(field) for field in required):
        return False
    try:
        payload = {
            "approval_id": str(record["approval_id"]),
            "manifest_digest": str(record["manifest_digest"]),
            "principal": str(record["principal"]),
            "session_id": str(record["session_id"]),
            "source": str(record["source"]),
            "status": "pending",
            "created_at": str(record["created_at"]),
            "expires_at": str(record["expires_at"]),
            "manifest_json": _json_dumps(record["manifest"]),
        }
        with _LOCK:
            with _connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO operative_approvals (
                        approval_id, manifest_digest, principal, session_id, source,
                        status, created_at, expires_at, manifest_json
                    ) VALUES (
                        :approval_id, :manifest_digest, :principal, :session_id, :source,
                        :status, :created_at, :expires_at, :manifest_json
                    )
                    """,
                    payload,
                )
                if cursor.rowcount != 1:
                    return False
                _append_approval_event(conn, payload, "created", payload["created_at"])
        return True
    except Exception:
        log.exception("operative approval proposal creation failed")
        return False


def get_operative_proposal(approval_id: str) -> dict[str, Any] | None:
    if not _ensure_schema():
        return None
    try:
        with _LOCK:
            with _connect() as conn:
                row = conn.execute(
                    "SELECT * FROM operative_approvals WHERE approval_id=?",
                    (str(approval_id),),
                ).fetchone()
                return _approval_row(row)
    except Exception:
        log.exception("operative approval lookup failed")
        return None


def consume_operative_approval(
    approval_id: str,
    *,
    manifest_digest: str,
    principal: str,
    session_id: str,
    source: str,
    run_id: str,
    now_iso: str,
    grant_expires_at: str,
    task_record: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Atomically approve, bind, and create one resumable execution run."""
    if not _ensure_schema():
        return None
    try:
        with _LOCK:
            with _connect() as conn:
                task_payload = {
                    "id": str(task_record.get("id") or ""),
                    "status": str(task_record.get("status") or "running"),
                    "created_at": str(task_record.get("created_at") or now_iso),
                    "updated_at": str(task_record.get("updated_at") or now_iso),
                    "finished_at": str(task_record.get("finished_at") or ""),
                    "payload_json": _json_dumps(dict(task_record)),
                }
                if task_payload["id"] != str(run_id):
                    return None
                conn.execute("BEGIN IMMEDIATE")
                updated = conn.execute(
                    """
                    UPDATE operative_approvals
                    SET status='consumed', approved_at=?, grant_expires_at=?,
                        consumed_at=?, run_id=?
                    WHERE approval_id=? AND status='pending'
                      AND manifest_digest=?
                      AND principal=? AND session_id=? AND source=?
                      AND expires_at>?
                    """,
                    (
                        now_iso,
                        str(grant_expires_at),
                        now_iso,
                        str(run_id),
                        str(approval_id),
                        str(manifest_digest),
                        str(principal),
                        str(session_id),
                        str(source),
                        now_iso,
                    ),
                ).rowcount
                if updated != 1:
                    return None
                row = conn.execute(
                    "SELECT * FROM operative_approvals WHERE approval_id=?",
                    (str(approval_id),),
                ).fetchone()
                record = _approval_row(row)
                if record is None:
                    raise sqlite3.DatabaseError("operative approval manifest is unreadable")
                conn.execute(
                    """
                    INSERT INTO tasks (
                        id, status, created_at, updated_at, finished_at, payload_json
                    ) VALUES (
                        :id, :status, :created_at, :updated_at, :finished_at, :payload_json
                    )
                    """,
                    task_payload,
                )
                _append_approval_event(conn, record, "approved", now_iso)
                _append_approval_event(conn, record, "consumed", now_iso)
                return record
    except Exception:
        log.exception("operative approval consumption failed")
        return None


def complete_operative_approval(
    approval_id: str,
    *,
    run_id: str,
    outcome: str,
    now_iso: str,
) -> bool:
    """Record a terminal execution outcome without exposing task content."""
    if not _ensure_schema():
        return False
    normalized_outcome = str(outcome or "failed").strip()[:100]
    try:
        with _LOCK:
            with _connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                updated = conn.execute(
                    """
                    UPDATE operative_approvals
                    SET completed_at=?, outcome=?
                    WHERE approval_id=? AND status='consumed' AND run_id=?
                      AND outcome=''
                    """,
                    (now_iso, normalized_outcome, str(approval_id), str(run_id)),
                ).rowcount
                if updated != 1:
                    return False
                row = conn.execute(
                    "SELECT * FROM operative_approvals WHERE approval_id=?",
                    (str(approval_id),),
                ).fetchone()
                record = _approval_row(row)
                if record is not None:
                    _append_approval_event(conn, record, normalized_outcome, now_iso)
                return True
    except Exception:
        log.exception("operative approval completion failed")
        return False


def terminalize_operative_task(
    task: Mapping[str, Any],
    *,
    approval_id: str,
    run_id: str,
    outcome: str,
    now_iso: str,
) -> bool:
    """Atomically persist a terminal task and its consumed approval outcome."""
    if not _ensure_schema():
        return False
    payload = _task_row_payload(task)
    if not payload["id"] or payload["id"] != str(run_id) or not approval_id:
        return False
    normalized_outcome = str(outcome or "failed").strip()[:100]
    try:
        with _LOCK:
            with _connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO tasks (
                        id, status, created_at, updated_at, finished_at, payload_json
                    ) VALUES (
                        :id, :status, :created_at, :updated_at, :finished_at, :payload_json
                    )
                    ON CONFLICT(id) DO UPDATE SET
                        status=excluded.status,
                        created_at=excluded.created_at,
                        updated_at=excluded.updated_at,
                        finished_at=excluded.finished_at,
                        payload_json=excluded.payload_json
                    """,
                    payload,
                )
                updated = conn.execute(
                    """
                    UPDATE operative_approvals
                    SET completed_at=?, outcome=?
                    WHERE approval_id=? AND status='consumed' AND run_id=?
                      AND outcome=''
                    """,
                    (now_iso, normalized_outcome, str(approval_id), str(run_id)),
                ).rowcount
                if updated != 1:
                    raise sqlite3.DatabaseError(
                        "consumed approval could not be terminalized"
                    )
                row = conn.execute(
                    "SELECT * FROM operative_approvals WHERE approval_id=?",
                    (str(approval_id),),
                ).fetchone()
                record = _approval_row(row)
                if record is None:
                    raise sqlite3.DatabaseError(
                        "terminal approval manifest is unreadable"
                    )
                _append_approval_event(conn, record, normalized_outcome, now_iso)
        return True
    except Exception:
        log.exception("operative task terminalization failed")
        return False


def cancel_operative_proposal(
    approval_id: str,
    *,
    principal: str,
    session_id: str,
    source: str,
    now_iso: str,
) -> bool:
    if not _ensure_schema():
        return False
    try:
        with _LOCK:
            with _connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                updated = conn.execute(
                    """
                    UPDATE operative_approvals
                    SET status='cancelled', cancelled_at=?
                    WHERE approval_id=? AND status IN ('pending', 'approved')
                      AND principal=? AND session_id=? AND source=?
                    """,
                    (
                        now_iso,
                        str(approval_id),
                        str(principal),
                        str(session_id),
                        str(source),
                    ),
                ).rowcount
                if updated != 1:
                    return False
                row = conn.execute(
                    "SELECT * FROM operative_approvals WHERE approval_id=?",
                    (str(approval_id),),
                ).fetchone()
                record = _approval_row(row)
                if record is not None:
                    _append_approval_event(conn, record, "cancelled", now_iso)
                return True
    except Exception:
        log.exception("operative approval cancellation failed")
        return False


def load_consumed_operative_approval(
    approval_id: str,
    *,
    run_id: str,
) -> dict[str, Any] | None:
    record = get_operative_proposal(approval_id)
    if not record:
        return None
    if (
        record.get("status") != "consumed"
        or record.get("run_id") != run_id
        or record.get("outcome")
    ):
        return None
    return record


def upsert_task(task: dict[str, Any]) -> bool:
    if not _ensure_schema():
        return False
    try:
        payload = _task_row_payload(task)
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


def claim_oldest_queued_task() -> "dict[str, Any] | None":
    """Atomically claim the oldest queued task.

    Marks it 'assigned' in a single transaction and returns the task dict.
    Returns None when nothing is queued.
    """
    if not _ensure_schema():
        return None
    try:
        with _LOCK:
            with _connect() as conn:
                row = conn.execute(
                    "SELECT id, payload_json FROM tasks "
                    "WHERE status='queued' ORDER BY created_at ASC LIMIT 1"
                ).fetchone()
                if not row:
                    return None
                task = json.loads(row["payload_json"])
                task["status"] = "assigned"
                updated = conn.execute(
                    "UPDATE tasks SET status='assigned', "
                    "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                    "payload_json=? WHERE id=? AND status='queued'",
                    (_json_dumps(task), row["id"]),
                ).rowcount
                return task if updated > 0 else None
    except Exception:
        log.exception("claim_oldest_queued_task failed")
        return None


def list_tasks_with_status(status: str, limit: int = 100) -> "list[dict[str, Any]]":
    """Return tasks with the given status, newest first."""
    if not _ensure_schema():
        return []
    try:
        with _LOCK:
            with _connect() as conn:
                rows = conn.execute(
                    "SELECT payload_json FROM tasks WHERE status=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (status, max(int(limit), 1)),
                ).fetchall()
                return [json.loads(r["payload_json"]) for r in rows]
    except Exception:
        log.exception("list_tasks_with_status failed for status=%s", status)
        return []


def update_task_status(task_id: str, status: str, *, result: str = "") -> bool:
    """Update a task's status and optionally its result field."""
    if not _ensure_schema():
        return False
    try:
        import datetime as _dt
        with _LOCK:
            with _connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if not row:
                    return False
                task = json.loads(row["payload_json"])
                task["status"] = status
                if result:
                    task["result"] = result
                if status in ("succeeded", "failed", "cancelled"):
                    task["finished_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
        return upsert_task(task)
    except Exception:
        log.exception("update_task_status failed for task_id=%s", task_id)
        return False


def checkpoint_step(
    run_id: str,
    step_number: int,
    description: str,
    tool: str,
    ok: bool,
    result: str,
) -> bool:
    """Append a step-completion event for a running task."""
    import datetime as _dt
    event = {
        "task_id": run_id,
        "type": "step_complete",
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "step_number": step_number,
        "description": description,
        "tool": tool,
        "ok": ok,
        "result": result[:500],
    }
    return append_event(event, event_index=step_number)


def find_interrupted_tasks() -> "list[dict[str, Any]]":
    """Return tasks still in 'running' status (i.e. the process died mid-task).

    Each returned dict includes a 'step_events' key with completed step data.
    """
    if not _ensure_schema():
        return []
    try:
        with _LOCK:
            with _connect() as conn:
                rows = conn.execute(
                    "SELECT id, payload_json FROM tasks WHERE status='running' "
                    "ORDER BY created_at DESC LIMIT 10"
                ).fetchall()
                if not rows:
                    return []
                task_ids = [row["id"] for row in rows]
                tasks = [json.loads(row["payload_json"]) for row in rows]
                placeholders = ",".join("?" for _ in task_ids)
                event_rows = conn.execute(
                    f"SELECT task_id, payload_json FROM task_events "
                    f"WHERE task_id IN ({placeholders}) AND event_type='step_complete' "
                    f"ORDER BY task_id, event_index ASC",
                    task_ids,
                ).fetchall()
                events_by_id: dict[str, list] = {tid: [] for tid in task_ids}
                for erow in event_rows:
                    events_by_id[erow["task_id"]].append(json.loads(erow["payload_json"]))
                for task in tasks:
                    task["step_events"] = events_by_id.get(task.get("id", ""), [])
                return tasks
    except Exception:
        log.exception("find_interrupted_tasks failed")
        return []


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
