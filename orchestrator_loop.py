"""
orchestrator_loop.py — Single-iteration autonomous orchestration loop.

Designed to be called on a schedule (every 3-5 minutes) by a Cowork scheduled
task.  Each call does ONE iteration of the loop:

    1. Harvest completed sessions → mark tasks done in WORK_QUEUE.json
    2. For each newly-completed task, ask local LLM to suggest follow-ups
    3. If active_count < max_concurrent: pick next QUEUED task
    4. Generate a full session prompt (via harness/prompt_generator.py)
    5. Write a launch record to LAUNCH_QUEUE.json
    6. Log all activity to MASTER_LOG.md

NOTE: Actual session launching (start_task) requires the Dispatch MCP which
only works inside Cowork.  This module writes to LAUNCH_QUEUE.json; a
companion Cowork scheduled task reads that file and fires start_task.

Entry point:
    python orchestrator_loop.py [--dry-run] [--max-concurrent N]
or as a library:
    from orchestrator_loop import run_loop
    run_loop(max_concurrent=3, dry_run=False)
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# ── Bootstrap: ensure repo root is on sys.path ────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from harness.session_tracker import SessionTracker  # noqa: E402

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

# ── File paths ─────────────────────────────────────────────────────────────────

WORK_QUEUE_PATH   = _REPO_ROOT / "WORK_QUEUE.json"
LAUNCH_QUEUE_PATH = _REPO_ROOT / "LAUNCH_QUEUE.json"
MASTER_LOG_PATH   = _REPO_ROOT / "MASTER_LOG.md"

# ── Models ─────────────────────────────────────────────────────────────────────

_FOLLOWUP_MODEL_DEFAULT = "qwen3:30b-a3b"
_FOLLOWUP_TIMEOUT       = 60   # seconds


# ── Public API ────────────────────────────────────────────────────────────────

def run_loop(max_concurrent: int = 3, dry_run: bool = False) -> dict[str, Any]:
    """
    Execute one iteration of the orchestration loop.

    Returns a summary dict:
    {
        "harvested":   int,   # tasks marked done
        "follow_ups":  int,   # new tasks enqueued
        "launched":    int,   # launch records written
        "active_now":  int,   # sessions still running
        "dry_run":     bool,
    }
    """
    now_str = _now()
    summary = {"harvested": 0, "follow_ups": 0, "launched": 0, "active_now": 0, "dry_run": dry_run}
    tracker = SessionTracker()

    _log_master(f"[orchestrator] loop start — max_concurrent={max_concurrent} dry_run={dry_run}")

    # ── Step 1: Harvest completed sessions ────────────────────────────────────
    completed = tracker.list_completed()
    newly_done: list[dict] = []
    for session in completed:
        sid     = session["session_id"]
        task_id = session.get("task_id", "")
        summary_text = session.get("result_summary", "")
        if _mark_task_done(task_id, summary_text):
            newly_done.append(session)
            summary["harvested"] += 1
            _log_master(f"[orchestrator] harvested {sid} → task {task_id} DONE")
            log.info("[Loop] Harvested session %s (task %s)", sid, task_id)
    tracker.purge_completed()

    # ── Step 2: Suggest follow-up tasks for completed work ────────────────────
    for session in newly_done:
        task_id = session.get("task_id", "")
        task    = _find_task(task_id)
        if task is None:
            continue
        follow_ups = _suggest_follow_ups(task, dry_run=dry_run)
        for ft in follow_ups:
            _enqueue_task(ft)
            summary["follow_ups"] += 1
            _log_master(f"[orchestrator] follow-up enqueued: {ft.get('id')} — {ft.get('title')}")

    # ── Step 3: Check headroom and pick next task ─────────────────────────────
    active_now = tracker.active_count()
    _log_master(f"[orchestrator] active sessions: {active_now}/{max_concurrent}")

    launched_this_run = 0
    while active_now + launched_this_run < max_concurrent:
        task = _pick_next_queued()
        if task is None:
            log.info("[Loop] No QUEUED tasks available")
            break

        # ── Step 4: Generate prompt ──────────────────────────────────────────
        repo_ctx = _build_repo_context()
        try:
            from harness.prompt_generator import generate_session_prompt
            prompt = generate_session_prompt(task, repo_ctx)
        except Exception as exc:
            log.error("[Loop] prompt_generator failed for %s: %s", task.get("id"), exc)
            # Build minimal fallback inline so we don't block the launch
            prompt = (
                f"Implement task {task.get('id')}: {task.get('title')}\n\n"
                f"{task.get('description', '')}\n\n"
                f"Acceptance criteria: {task.get('acceptance_criteria', [])}"
            )

        # ── Step 5: Write launch record ──────────────────────────────────────
        session_id = _session_id_for_task(task)
        launch_record = {
            "task_id":    task["id"],
            "session_id": session_id,
            "prompt":     prompt,
            "queued_at":  now_str,
            "status":     "pending",       # Cowork companion sets to "fired" after start_task
            "domain":     task.get("domain", "general"),
            "assigned_ai": task.get("assigned_ai", "claude"),
        }

        if dry_run:
            log.info("[Loop][DRY-RUN] Would launch session %s for task %s\nPrompt preview:\n%s",
                     session_id, task["id"], prompt[:400])
        else:
            _write_launch_record(launch_record)
            tracker.claim(task["id"], session_id)
            _mark_task_in_progress(task["id"], session_id)

        summary["launched"] += 1
        launched_this_run  += 1
        _log_master(
            f"[orchestrator] {'[DRY-RUN] ' if dry_run else ''}"
            f"launch queued: {session_id} → {task['id']} — {task.get('title')}"
        )

    summary["active_now"] = tracker.active_count() + launched_this_run
    _log_master(
        f"[orchestrator] loop done — harvested={summary['harvested']} "
        f"launched={summary['launched']} follow_ups={summary['follow_ups']} "
        f"active={summary['active_now']}"
    )
    return summary


# ── WORK_QUEUE helpers ────────────────────────────────────────────────────────

def _load_queue() -> list[dict[str, Any]]:
    if not WORK_QUEUE_PATH.exists():
        return []
    try:
        with open(WORK_QUEUE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as exc:
        log.error("[Loop] Could not read WORK_QUEUE.json: %s", exc)
        return []


def _save_queue(queue: list[dict[str, Any]]) -> None:
    tmp = str(WORK_QUEUE_PATH) + ".loop.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(queue, f, indent=2)
        os.replace(tmp, str(WORK_QUEUE_PATH))
    except Exception as exc:
        log.error("[Loop] Could not save WORK_QUEUE.json: %s", exc)


def _find_task(task_id: str) -> dict | None:
    return next((t for t in _load_queue() if t.get("id") == task_id), None)


def _pick_next_queued() -> dict | None:
    queue = _load_queue()
    # Respect explicit priority field if present (lower = higher priority)
    queued = [t for t in queue if t.get("status") == "queued"]
    if not queued:
        return None
    queued.sort(key=lambda t: (t.get("priority", 99), t.get("created_at", "")))
    return queued[0]


def _mark_task_done(task_id: str, result_summary: str) -> bool:
    """Mark task as done; return True if the task was found and updated."""
    if not task_id:
        return False
    queue = _load_queue()
    for task in queue:
        if task.get("id") == task_id:
            task["status"]         = "done"
            task["result_summary"] = result_summary
            task["completed_at"]   = _now()
            _save_queue(queue)
            return True
    return False


def _mark_task_in_progress(task_id: str, session_id: str) -> None:
    queue = _load_queue()
    for task in queue:
        if task.get("id") == task_id:
            task["status"]      = "in_progress"
            task["assigned_to"] = session_id
            task["assigned_at"] = _now()
            break
    _save_queue(queue)


def _enqueue_task(task: dict[str, Any]) -> None:
    queue = _load_queue()
    # Deduplicate by id
    if any(t.get("id") == task.get("id") for t in queue):
        return
    task.setdefault("status", "queued")
    task.setdefault("created_at", _now())
    queue.append(task)
    _save_queue(queue)


# ── LAUNCH_QUEUE helpers ──────────────────────────────────────────────────────

def _load_launch_queue() -> list[dict[str, Any]]:
    if not LAUNCH_QUEUE_PATH.exists():
        return []
    try:
        with open(LAUNCH_QUEUE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as exc:
        log.error("[Loop] Could not read LAUNCH_QUEUE.json: %s", exc)
        return []


def _write_launch_record(record: dict[str, Any]) -> None:
    """Append a launch record to LAUNCH_QUEUE.json (atomic write)."""
    queue = _load_launch_queue()
    # Deduplicate by task_id+status=pending
    existing_ids = {r["task_id"] for r in queue if r.get("status") == "pending"}
    if record["task_id"] in existing_ids:
        log.debug("[Loop] task %s already pending in LAUNCH_QUEUE — skipping", record["task_id"])
        return
    queue.append(record)
    tmp = str(LAUNCH_QUEUE_PATH) + ".loop.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(queue, f, indent=2)
        os.replace(tmp, str(LAUNCH_QUEUE_PATH))
    except Exception as exc:
        log.error("[Loop] Could not write LAUNCH_QUEUE.json: %s", exc)


# ── Follow-up task generation ─────────────────────────────────────────────────

_FOLLOWUP_SYSTEM = """\
You are an engineering lead at Jarvis AI.  Given a completed task, suggest 0-3 \
logical follow-up tasks that would naturally come next.  Respond with a JSON array \
only, no explanation.  Each element: \
{"id": "TASK-NNN", "title": "...", "description": "...", \
"files_hint": [...], "acceptance_criteria": [...], "domain": "...", \
"assigned_ai": "claude", "priority": 2}.  \
If no follow-ups are needed, return an empty array [].
"""


def _suggest_follow_ups(task: dict[str, Any], dry_run: bool = False) -> list[dict[str, Any]]:
    """Ask local LLM to propose follow-up tasks for a completed task."""
    if dry_run:
        log.info("[Loop][DRY-RUN] skipping follow-up generation for %s", task.get("id"))
        return []

    prompt = (
        f"Completed task:\n"
        f"ID: {task.get('id')}\n"
        f"Title: {task.get('title')}\n"
        f"Description: {task.get('description', '')}\n"
        f"Result: {task.get('result_summary', 'done')}\n\n"
        f"Suggest follow-up tasks (JSON array):"
    )

    try:
        from brains.brain_ollama import ask_local  # type: ignore[import]
        try:
            from config import LOCAL_REASONING  # type: ignore[import]
            model = LOCAL_REASONING or _FOLLOWUP_MODEL_DEFAULT
        except Exception:
            model = _FOLLOWUP_MODEL_DEFAULT

        raw = ask_local(prompt, model=model, system=_FOLLOWUP_SYSTEM, timeout=_FOLLOWUP_TIMEOUT)
        # Strip markdown fences if the model wrapped the JSON
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        follow_ups = json.loads(raw)
        if not isinstance(follow_ups, list):
            return []
        # Assign unique IDs if missing
        existing_ids = {t.get("id") for t in _load_queue()}
        result = []
        for ft in follow_ups:
            if not isinstance(ft, dict):
                continue
            if not ft.get("id") or ft["id"] in existing_ids:
                ft["id"] = _new_task_id()
            result.append(ft)
        return result
    except Exception as exc:
        log.warning("[Loop] follow-up generation failed: %s", exc)
        return []


# ── Repo context helper ───────────────────────────────────────────────────────

def _build_repo_context() -> dict[str, Any]:
    """Gather lightweight repo context for prompt generation."""
    ctx: dict[str, Any] = {"recent_commits": [], "test_count": 0, "active_files": []}

    # Recent git commits
    try:
        import subprocess
        result = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "log", "--oneline", "-5"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            ctx["recent_commits"] = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
    except Exception:
        pass

    # Test count
    try:
        tests_dir = _REPO_ROOT / "tests"
        if tests_dir.exists():
            ctx["test_count"] = sum(1 for _ in tests_dir.glob("test_*.py"))
    except Exception:
        pass

    # Recently modified source files (last 7 days)
    try:
        import subprocess
        result = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "diff", "--name-only", "HEAD~5", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            ctx["active_files"] = [
                f.strip() for f in result.stdout.strip().splitlines()
                if f.strip() and f.strip().endswith(".py")
            ][:10]
    except Exception:
        pass

    return ctx


# ── Utility ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _session_id_for_task(task: dict[str, Any]) -> str:
    """Derive a deterministic session name from the task."""
    domain = task.get("domain", "general")
    ai     = task.get("assigned_ai", "claude")
    tid    = task.get("id", "task").lower().replace("-", "")
    return f"jarvis-{domain}-{ai}-{tid}"


def _new_task_id() -> str:
    """Generate a unique task ID based on existing queue length."""
    queue = _load_queue()
    existing_nums = []
    for t in queue:
        tid = t.get("id", "")
        if tid.startswith("TASK-"):
            try:
                existing_nums.append(int(tid.split("-")[1]))
            except (IndexError, ValueError):
                pass
    next_num = max(existing_nums, default=0) + 1
    return f"TASK-{next_num:03d}"


def _log_master(message: str) -> None:
    """Append a timestamped line to MASTER_LOG.md."""
    line = f"[{datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC] {message}\n"
    try:
        with open(MASTER_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as exc:
        log.debug("[Loop] Could not write MASTER_LOG.md: %s", exc)


# ── CLI entrypoint ────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Jarvis autonomous orchestration loop (one iteration)")
    p.add_argument("--dry-run",        action="store_true", help="Print actions without mutating state")
    p.add_argument("--max-concurrent", type=int, default=3,  help="Maximum active sessions (default 3)")
    p.add_argument("--verbose",        action="store_true", help="Enable DEBUG logging")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    result = run_loop(max_concurrent=args.max_concurrent, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
