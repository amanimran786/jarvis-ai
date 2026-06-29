"""
harness/cowork_launcher.py — Cowork scheduled-task bridge for the autonomous loop.

Reads LAUNCH_QUEUE.json and fires pending session launches.
Called by a scheduled task every 5 minutes.

Since start_task is a Cowork MCP tool that cannot be called from Python directly,
this module writes each pending launch as a file in PENDING_SESSIONS/{task_id}.txt
containing the full prompt.  A human or the Cowork scheduler reads these files and
fires the actual sessions.

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

    For each entry with status="pending":
      1. Log it to MASTER_LOG.md
      2. Mark it status="fired", add fired_at timestamp
      3. Write the prompt to PENDING_SESSIONS/{task_id}.txt for human/Cowork pickup

    Returns the list of entries that were processed (state mutated in-place).
    The queue file is updated atomically after all entries are processed.
    """
    queue_path = Path(queue_path)
    queue      = _load_queue(queue_path)

    processed: list[dict] = []
    pending    = [e for e in queue if e.get("status") == "pending"]

    if not pending:
        log.info("[CoworkLauncher] No pending entries in launch queue.")
        return []

    # Ensure output directory exists
    pending_dir = _resolve_pending_dir(queue_path)
    pending_dir.mkdir(parents=True, exist_ok=True)

    now = _now()

    for entry in pending:
        task_id    = entry.get("task_id", "unknown")
        session_id = entry.get("session_id", "unknown")
        prompt     = entry.get("prompt", "")
        domain     = entry.get("domain", "general")
        ai         = entry.get("assigned_ai", "claude")

        # 1. Log to MASTER_LOG.md
        _log_master(
            f"[cowork_launcher] firing session {session_id} for task {task_id} "
            f"(domain={domain}, ai={ai})",
            queue_path,
        )
        log.info("[CoworkLauncher] Processing task %s → session %s", task_id, session_id)

        # 2. Mark as fired
        entry["status"]   = "fired"
        entry["fired_at"] = now

        # 3. Write prompt file for Cowork/human pickup
        prompt_file = pending_dir / f"{task_id}.txt"
        _write_prompt_file(prompt_file, entry, prompt)

        processed.append(entry)

    # Persist the updated queue atomically
    _save_queue(queue_path, queue)

    log.info("[CoworkLauncher] Processed %d pending entries.", len(processed))
    return processed


def run() -> None:
    """
    Scheduler entry point.

    1. Calls orchestrator_loop.run_loop() to harvest completed sessions and
       queue new launch records into LAUNCH_QUEUE.json.
    2. Calls process_launch_queue() to materialise pending records as
       PENDING_SESSIONS/*.txt files ready for Cowork pickup.
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
    log.info("[CoworkLauncher] run() done — fired %d sessions.", len(processed))


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
    tmp = str(path) + ".launcher.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(queue, f, indent=2)
        os.replace(tmp, str(path))
    except Exception as exc:
        log.error("[CoworkLauncher] Could not save %s: %s", path, exc)
        try:
            os.remove(tmp)
        except OSError:
            pass


def _write_prompt_file(path: Path, entry: dict[str, Any], prompt: str) -> None:
    """Write a self-contained prompt file that Cowork/human can pick up."""
    task_id    = entry.get("task_id", "unknown")
    session_id = entry.get("session_id", "unknown")
    domain     = entry.get("domain", "general")
    ai         = entry.get("assigned_ai", "claude")
    queued_at  = entry.get("queued_at", "unknown")
    fired_at   = entry.get("fired_at", _now())

    header = (
        f"# Pending Session Launch\n"
        f"# task_id:    {task_id}\n"
        f"# session_id: {session_id}\n"
        f"# domain:     {domain}\n"
        f"# ai:         {ai}\n"
        f"# queued_at:  {queued_at}\n"
        f"# fired_at:   {fired_at}\n"
        f"# ---\n\n"
    )
    try:
        path.write_text(header + prompt, encoding="utf-8")
        log.debug("[CoworkLauncher] Wrote prompt file: %s", path)
    except Exception as exc:
        log.error("[CoworkLauncher] Failed to write %s: %s", path, exc)


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
