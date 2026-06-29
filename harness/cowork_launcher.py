"""
harness/cowork_launcher.py — Cowork scheduled-task bridge for the autonomous loop.

Reads LAUNCH_QUEUE.json and fires pending session launches.
Called by a scheduled task every 5 minutes.

Since start_task is a Cowork MCP tool that cannot be called from Python directly,
this module writes each launch attempt as a self-contained JSON envelope in
PENDING_SESSIONS/{attempt_id}.json. A human or the Cowork scheduler reads these
files and fires the actual sessions.

Public API:
    process_launch_queue(queue_path) -> list[dict]
        Returns list of entries that were processed (status was "pending").

    run() -> None
        Entry point.  Harvests completions via orchestrator_loop.run_loop(),
        then processes the launch queue.

CLI:
    python -m harness.cowork_launcher
    python harness/cowork_launcher.py
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any

# ── Bootstrap: ensure repo root is on sys.path ────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

# ── File paths (overridable for tests) ────────────────────────────────────────
LAUNCH_QUEUE_PATH    = _REPO_ROOT / "LAUNCH_QUEUE.json"
MASTER_LOG_PATH      = _REPO_ROOT / "MASTER_LOG.md"
PENDING_SESSIONS_DIR = _REPO_ROOT / "PENDING_SESSIONS"


# ── Public API ────────────────────────────────────────────────────────────────

def process_launch_queue(queue_path: str | Path = LAUNCH_QUEUE_PATH) -> list[dict]:
    """
    Read the launch queue and process all pending entries.

    For each pending or previously failed entry:
      1. Atomically materialize PENDING_SESSIONS/{attempt_id}.json.
      2. Mark it handoff_ready only after the pickup artifact exists.
      3. Record artifact failures as launch_error so the attempt remains retryable.

    Returns entries whose pickup artifact exists and ready state was persisted.
    The queue file is updated atomically after all candidates are processed.
    """
    queue_path = Path(queue_path)
    queue      = _load_queue(queue_path)

    processed: list[dict] = []
    pending = [
        entry
        for entry in queue
        if isinstance(entry, dict)
        and entry.get("status") in {"pending", "launch_error"}
    ]

    if not pending:
        log.info("[CoworkLauncher] No pending entries in launch queue.")
        return []

    # Ensure output directory exists
    pending_dir = _resolve_pending_dir(queue_path)
    pending_dir.mkdir(parents=True, exist_ok=True)

    for entry in pending:
        task_id    = entry.get("task_id", "unknown")
        session_id = entry.get("session_id", "unknown")
        prompt     = entry.get("prompt", "")

        log.info("[CoworkLauncher] Materializing task %s -> session %s", task_id, session_id)

        try:
            prompt_file = pending_dir / _artifact_name(entry)
            _write_prompt_file(prompt_file, entry, prompt)
            if not prompt_file.is_file():
                raise OSError(f"pickup artifact was not materialized: {prompt_file}")
        except Exception as exc:
            entry["status"] = "launch_error"
            entry.pop("fired_at", None)
            entry["launch_error"] = f"{type(exc).__name__}: {exc}"
            entry["launch_error_at"] = _now()
            log.error(
                "[CoworkLauncher] Pickup artifact failed for attempt %s: %s",
                entry.get("attempt_id", "unknown"),
                exc,
            )
            continue

        entry["status"] = "handoff_ready"
        entry["handoff_ready_at"] = _now()
        entry.pop("fired_at", None)
        entry.pop("launch_error", None)
        entry.pop("launch_error_at", None)
        processed.append(entry)

    # Persist the updated queue atomically
    _save_queue(queue_path, queue)

    for entry in processed:
        _log_master(
            f"[cowork_launcher] pickup ready for session {entry.get('session_id', 'unknown')} "
            f"task {entry.get('task_id', 'unknown')} attempt "
            f"{entry.get('attempt_id', 'unknown')} "
            f"(domain={entry.get('domain', 'general')}, "
            f"ai={entry.get('assigned_ai', 'claude')})",
            queue_path,
        )

    log.info("[CoworkLauncher] Processed %d pending entries.", len(processed))
    return processed


def run() -> None:
    """
    Scheduler entry point.

    1. Calls orchestrator_loop.run_loop() to harvest completed sessions and
       queue new launch records into LAUNCH_QUEUE.json.
    2. Calls process_launch_queue() to materialise pending records as
       PENDING_SESSIONS/*.json envelopes ready for Cowork pickup.
    """
    log.info("[CoworkLauncher] run() started")

    # Step 1: harvest completions and generate new launch records
    try:
        from orchestrator_loop import run_loop  # noqa: PLC0415
        summary = run_loop()
        log.info(
            "[CoworkLauncher] orchestrator_loop.run_loop() summary: %s",
            json.dumps(summary),
        )
    except Exception as exc:
        log.error("[CoworkLauncher] orchestrator_loop.run_loop() failed: %s", exc)

    # Step 2: fire pending entries
    processed = process_launch_queue()
    log.info("[CoworkLauncher] run() done — prepared %d handoffs.", len(processed))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_queue(path: Path) -> list[dict[str, Any]]:
    """Return the launch queue list, or [] if the file is missing/corrupt."""
    if not path.exists():
        log.debug("[CoworkLauncher] launch queue not found at %s — treating as empty.", path)
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as exc:
        log.error("[CoworkLauncher] Could not read %s: %s", path, exc)
        return []


def _save_queue(path: Path, queue: list[dict[str, Any]]) -> None:
    """Atomically write the queue back to disk."""
    _atomic_write_json(path, queue)


def _artifact_name(entry: dict[str, Any]) -> str:
    """Return a path-safe, attempt-specific artifact name."""
    attempt_id = entry.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ValueError("launch record requires a non-empty attempt_id")
    if attempt_id in {".", ".."} or Path(attempt_id).name != attempt_id:
        raise ValueError(f"unsafe attempt_id: {attempt_id!r}")
    return f"{attempt_id}.json"


def _write_prompt_file(path: Path, entry: dict[str, Any], prompt: Any) -> None:
    """Atomically materialize an immutable, self-contained pickup envelope."""
    required_fields = (
        "task_id",
        "attempt_id",
        "session_id",
        "contract_sha256",
        "task_spec",
        "repo_path",
        "base_ref",
    )
    missing = [field for field in required_fields if field not in entry]
    if "prompt" not in entry:
        missing.append("prompt")
    if missing:
        raise ValueError(f"launch record missing fields: {', '.join(missing)}")
    if not isinstance(entry["task_spec"], dict):
        raise ValueError("launch record task_spec must be an object")

    expected = {
        "schema_version": 1,
        "task_id": entry["task_id"],
        "attempt_id": entry["attempt_id"],
        "session_id": entry["session_id"],
        "contract_sha256": entry["contract_sha256"],
        "task_spec": entry["task_spec"],
        "prompt": prompt,
        "repo_path": entry["repo_path"],
        "base_ref": entry["base_ref"],
        "queued_at": entry.get("queued_at"),
        "domain": entry.get("domain"),
        "assigned_ai": entry.get("assigned_ai"),
    }

    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OSError(f"existing pickup artifact is unreadable: {path}") from exc
        if not isinstance(existing, dict) or any(
            existing.get(key) != value for key, value in expected.items()
        ):
            raise OSError(f"existing pickup artifact conflicts with attempt: {path}")
        log.debug("[CoworkLauncher] Reusing pickup artifact: %s", path)
        return

    envelope = dict(expected)
    envelope["materialized_at"] = _now()
    _atomic_write_json(path, envelope)
    log.debug("[CoworkLauncher] Wrote pickup artifact: %s", path)


def _atomic_write_json(path: Path, value: Any) -> None:
    """Write JSON to a sibling temp file and atomically replace the target."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "x", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _resolve_pending_dir(queue_path: Path) -> Path:
    """Return PENDING_SESSIONS dir co-located with the queue file's parent."""
    return queue_path.parent / "PENDING_SESSIONS"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _log_master(message: str, queue_path: Path | None = None) -> None:
    """Append a timestamped line to MASTER_LOG.md."""
    log_path = (
        queue_path.parent / "MASTER_LOG.md"
        if queue_path is not None
        else MASTER_LOG_PATH
    )
    line = f"[{datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC] {message}\n"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as exc:
        log.debug("[CoworkLauncher] Could not write MASTER_LOG.md: %s", exc)


# ── CLI entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run()
