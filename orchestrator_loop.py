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
import subprocess
import sys
from pathlib import Path
from typing import Any

# ── Bootstrap: ensure repo root is on sys.path ────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from harness.session_tracker import SessionTracker  # noqa: E402
from harness.completion_verifier import (  # noqa: E402
    CompletionEvidenceError,
    collect_completion_evidence,
)
from harness.retry_policy import RetryAction, RetryDecision, decide_retry  # noqa: E402
from harness.task_contract import (  # noqa: E402
    AttemptRecord,
    AttemptStore,
    CompletionVerdict,
    ContractError,
    TaskContract,
    TaskSpec,
    approval_logged,
    contract_for_task,
    evaluate_completion,
    validate_contract,
)

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
ATTEMPT_LOG_PATH  = Path(
    os.getenv(
        "JARVIS_ORCHESTRATOR_ATTEMPT_LOG",
        str(Path.home() / "Library" / "Application Support" / "Jarvis" / "orchestrator" / "attempts.jsonl"),
    )
)

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
        "blocked":     int,   # launch-time contract/checkpoint failures
        "unverified":  int,   # completion claims missing loop evidence
        "rejected":    int,   # completion evidence failed a hard gate
        "retried":     int,   # failed attempts returned to the executor queue
        "active_now":  int,   # sessions still running
        "dry_run":     bool,
    }
    """
    now_str = _now()
    summary = {
        "harvested": 0,
        "follow_ups": 0,
        "launched": 0,
        "blocked": 0,
        "unverified": 0,
        "rejected": 0,
        "retried": 0,
        "active_now": 0,
        "dry_run": dry_run,
    }
    tracker = SessionTracker()
    attempt_store = AttemptStore(ATTEMPT_LOG_PATH)

    _log_master(f"[orchestrator] loop start — max_concurrent={max_concurrent} dry_run={dry_run}")

    # ── Step 1: Harvest completed sessions ────────────────────────────────────
    completed = tracker.list_completed()
    newly_done: list[dict] = []
    for session in completed:
        sid     = session["session_id"]
        task_id = session.get("task_id", "")
        summary_text = session.get("result_summary", "")
        task = _find_task(task_id)
        if task is None:
            _log_master(f"[orchestrator] completion ignored — task {task_id} not found")
            continue
        try:
            spec = TaskSpec.from_queue_task(task)
        except ContractError as exc:
            _mark_task_verification(task_id, "blocked", "invalid_contract", [str(exc)], {})
            summary["rejected"] += 1
            continue

        claimed_evidence = session.get("completion_evidence") or {}
        evidence: dict[str, Any] = {}
        runtime_failure = str(session.get("runtime_failure_class") or "")
        if runtime_failure:
            verdict = CompletionVerdict(
                "rejected",
                runtime_failure,
                (summary_text or "runtime execution failed",),
            )
        elif session.get("repo_path") and session.get("base_ref"):
            try:
                evidence = collect_completion_evidence(
                    spec,
                    session["repo_path"],
                    session["base_ref"],
                )
            except CompletionEvidenceError as exc:
                _log_master(
                    f"[orchestrator] evidence collection failed for {task_id}: {exc}"
                )
        else:
            # Compatibility for sessions created before launch provenance was stored.
            # Current launches always use loop-collected evidence above.
            evidence = claimed_evidence

        stored_hash = str(session.get("contract_sha256") or "")
        if runtime_failure:
            pass
        elif stored_hash and stored_hash != spec.contract_hash:
            verdict = CompletionVerdict(
                "rejected",
                "contract_mismatch",
                ("task contract changed after dispatch",),
            )
        else:
            verdict = evaluate_completion(spec, evidence)

        attempt_id = str(session.get("attempt_id") or f"untracked_{sid}")
        attempt_number = int(session.get("attempt_number") or 1)
        completion_checkpoint = AttemptRecord.checkpoint(
            spec,
            sid,
            attempt_id,
            phase="completion_gate",
            status=verdict.status,
            evidence=evidence,
            failure_class=verdict.failure_class,
            attempt_number=attempt_number,
        )
        try:
            attempt_store.append(completion_checkpoint)
        except OSError as exc:
            verdict = CompletionVerdict(
                "unverified",
                "checkpoint_failure",
                (f"completion checkpoint write failed: {exc}",),
            )

        if verdict.passed and _mark_task_done(task_id, summary_text):
            newly_done.append(session)
            summary["harvested"] += 1
            _log_master(f"[orchestrator] verified {sid} → task {task_id} DONE")
            log.info("[Loop] Harvested session %s (task %s)", sid, task_id)
        else:
            remaining = evidence.get("remaining_budget", {}) if isinstance(evidence, dict) else {}
            decision = decide_retry(
                verdict.failure_class,
                attempt_number,
                spec.budget,
                int(remaining.get("wall_time_seconds", spec.budget.wall_time_seconds)),
                int(remaining.get("tool_calls", spec.budget.tool_calls)),
            )
            retry_checkpoint = AttemptRecord.checkpoint(
                spec,
                sid,
                attempt_id,
                phase="retry_policy",
                status=decision.action.value,
                evidence={
                    "target": decision.target.value,
                    "reason": decision.reason,
                    "remaining_attempts": decision.remaining_attempts,
                },
                failure_class=decision.failure_class.value,
                attempt_number=attempt_number,
            )
            try:
                attempt_store.append(retry_checkpoint)
            except OSError as exc:
                _log_master(
                    f"[orchestrator] retry checkpoint write failed for {task_id}: {exc}"
                )
            if decision.action is RetryAction.RETRY:
                _mark_task_retry(task_id, decision)
                queue_status = "queued"
                summary["retried"] += 1
            elif decision.action is RetryAction.ROUTE_TO_VERIFIER:
                queue_status = "unverified"
                _mark_task_verification(
                    task_id,
                    queue_status,
                    verdict.failure_class,
                    list(verdict.reasons),
                    evidence,
                    next_action=decision.action.value,
                )
                summary["unverified"] += 1
            else:
                queue_status = "blocked"
                _mark_task_verification(
                    task_id,
                    queue_status,
                    verdict.failure_class,
                    list(verdict.reasons),
                    evidence,
                    next_action=decision.action.value,
                )
                summary["rejected"] += 1
            _log_master(
                f"[orchestrator] {sid} → task {task_id} {queue_status.upper()} "
                f"({verdict.failure_class}; {decision.action.value})"
            )
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

        try:
            spec = TaskSpec.from_queue_task(task)
        except ContractError as exc:
            _mark_task_blocked(task, f"invalid task contract: {exc}")
            summary["blocked"] += 1
            _log_master(f"[orchestrator] task blocked — invalid contract: {exc}")
            continue
        if spec.legacy_adapter:
            # Typed task contract gate (CODEX-8): a legacy queue entry may only
            # execute autonomously if an explicit TaskContract covers it.
            contract = _resolve_typed_contract(task, spec)
            if contract is None:
                _mark_task_blocked(
                    task,
                    "autonomous execution requires an explicit typed task contract",
                )
                summary["blocked"] += 1
                _log_master(
                    f"[orchestrator] {spec.task_id} blocked — legacy contract is not executable"
                )
                continue
            is_valid, contract_errors = validate_contract(contract)
            if not is_valid:
                _mark_task_blocked(
                    task,
                    f"typed task contract invalid: {'; '.join(contract_errors)}",
                )
                summary["blocked"] += 1
                _log_master(
                    f"[orchestrator] {spec.task_id} blocked — typed contract "
                    f"{contract.task_id} invalid: {'; '.join(contract_errors)}"
                )
                continue
            if contract.requires_approval and not approval_logged(contract.task_id):
                _mark_task_awaiting_approval(task)
                summary["blocked"] += 1
                _log_master(
                    f"[orchestrator] {spec.task_id} awaiting approval — contract "
                    f"{contract.task_id} requires human sign-off"
                )
                continue
            _mark_task_contract_validated(task, contract)
            _log_master(
                f"[orchestrator] {spec.task_id} typed contract {contract.task_id} "
                f"v{contract.contract_version} validated — dispatching"
            )
            # Rebuild the spec as contract-backed so the runtime launcher's
            # legacy_adapter check (harness/runtime_launcher.py) accepts it.
            spec = TaskSpec.from_queue_task({**spec.to_dict(), "legacy_adapter": False})

        # ── Step 4: Generate prompt ──────────────────────────────────────────
        repo_ctx = _build_repo_context()
        try:
            from harness.prompt_generator import generate_session_prompt
            prompt = generate_session_prompt(spec.to_dict(), repo_ctx)
        except Exception as exc:
            log.error("[Loop] prompt_generator failed for %s: %s", spec.task_id, exc)
            _mark_task_blocked(task, f"contract rendering failed: {exc}")
            summary["blocked"] += 1
            _log_master(f"[orchestrator] {spec.task_id} blocked — contract rendering failed")
            continue

        # ── Step 5: Write launch record ──────────────────────────────────────
        session_id = _session_id_for_task(spec.to_dict())
        attempt_number = int(task.get("attempt_number") or 1)
        attempt = AttemptRecord.dispatched(
            spec,
            session_id,
            attempt_number=attempt_number,
        )
        try:
            base_ref = _git_head()
        except RuntimeError as exc:
            _mark_task_blocked(task, f"could not capture Git baseline: {exc}")
            summary["blocked"] += 1
            continue
        launch_record = {
            "task_id":    spec.task_id,
            "session_id": session_id,
            "attempt_id": attempt.attempt_id,
            "attempt_number": attempt_number,
            "contract_sha256": spec.contract_hash,
            "task_spec":  spec.to_dict(),
            "repo_path":  str(_REPO_ROOT),
            "base_ref":   base_ref,
            "prompt":     prompt,
            "queued_at":  now_str,
            "status":     "pending",       # companion materializes a durable handoff envelope
            "domain":     spec.domain,
            "assigned_ai": spec.assigned_ai,
        }

        if dry_run:
            log.info("[Loop][DRY-RUN] Would launch session %s for task %s\nPrompt preview:\n%s",
                     session_id, spec.task_id, prompt[:400])
        else:
            try:
                attempt_store.append(attempt)
            except OSError as exc:
                _mark_task_blocked(task, f"checkpoint write failed: {exc}")
                summary["blocked"] += 1
                _log_master(
                    f"[orchestrator] {spec.task_id} blocked — checkpoint write failed: {exc}"
                )
                continue
            if not _write_launch_record(launch_record):
                delivery_failure = AttemptRecord.checkpoint(
                    spec,
                    session_id,
                    attempt.attempt_id,
                    phase="dispatch_delivery",
                    status="failed",
                    evidence={"target": str(LAUNCH_QUEUE_PATH)},
                    failure_class="infrastructure_failure",
                    attempt_number=attempt_number,
                )
                try:
                    attempt_store.append(delivery_failure)
                except OSError as exc:
                    _log_master(
                        f"[orchestrator] launch failure checkpoint write failed for "
                        f"{spec.task_id}: {exc}"
                    )
                _mark_task_blocked(task, "launch queue delivery failed")
                summary["blocked"] += 1
                _log_master(
                    f"[orchestrator] {spec.task_id} blocked — launch queue delivery failed"
                )
                continue
            tracker.claim(
                spec.task_id,
                session_id,
                attempt_id=attempt.attempt_id,
                contract_sha256=spec.contract_hash,
                attempt_number=attempt_number,
                repo_path=str(_REPO_ROOT),
                base_ref=base_ref,
            )
            _mark_task_in_progress(spec.task_id, session_id)

        summary["launched"] += 1
        launched_this_run  += 1
        _log_master(
            f"[orchestrator] {'[DRY-RUN] ' if dry_run else ''}"
            f"launch queued: {session_id} → {spec.task_id} — {spec.title}"
        )

    summary["active_now"] = (
        active_now + launched_this_run if dry_run else tracker.active_count()
    )
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
    return next((t for t in _load_queue() if _task_identity(t) == task_id), None)


def _task_identity(task: dict[str, Any]) -> str:
    try:
        return TaskSpec.from_queue_task(task).task_id
    except ContractError:
        return str(task.get("id") or "")


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
        if _task_identity(task) == task_id:
            task["status"]         = "done"
            task["result_summary"] = result_summary
            task["completed_at"]   = _now()
            _save_queue(queue)
            return True
    return False


def _mark_task_in_progress(task_id: str, session_id: str) -> None:
    queue = _load_queue()
    for task in queue:
        if _task_identity(task) == task_id:
            task["status"]      = "in_progress"
            task["assigned_to"] = session_id
            task["assigned_at"] = _now()
            break
    _save_queue(queue)


def _mark_task_verification(
    task_id: str,
    status: str,
    failure_class: str,
    reasons: list[str],
    evidence: dict[str, Any],
    *,
    next_action: str = "",
) -> None:
    queue = _load_queue()
    for task in queue:
        if _task_identity(task) == task_id:
            task["status"] = status
            task["verification_failure_class"] = failure_class
            task["verification_reasons"] = reasons
            task["completion_evidence"] = evidence
            task["next_action"] = next_action
            task["verified_at"] = _now()
            break
    _save_queue(queue)


def _mark_task_retry(task_id: str, decision: RetryDecision) -> None:
    queue = _load_queue()
    for task in queue:
        if _task_identity(task) == task_id:
            task["status"] = "queued"
            task["attempt_number"] = decision.attempt_number + 1
            task["retry_failure_class"] = decision.failure_class.value
            task["retry_reason"] = decision.reason
            task["assigned_to"] = None
            task["assigned_at"] = None
            break
    _save_queue(queue)


def _mark_task_blocked(task_to_block: dict[str, Any], reason: str) -> None:
    queue = _load_queue()
    identity = _task_identity(task_to_block)
    for task in queue:
        if task == task_to_block or (identity and _task_identity(task) == identity):
            task["status"] = "blocked"
            task["blocked_reason"] = reason
            task["blocked_at"] = _now()
            break
    _save_queue(queue)


def _resolve_typed_contract(task: dict[str, Any], spec: TaskSpec) -> TaskContract | None:
    """Find the TaskContract covering a queue entry.

    Lookup order: explicit contract_id stamp → derived spec identity →
    session_name (only usable while a lane has a single contracted task).
    """
    for key in (task.get("contract_id"), spec.task_id, task.get("session_name")):
        if key:
            contract = contract_for_task(str(key))
            if contract is not None:
                return contract
    return None


def _mark_task_awaiting_approval(task_to_mark: dict[str, Any]) -> None:
    queue = _load_queue()
    identity = _task_identity(task_to_mark)
    for task in queue:
        if task == task_to_mark or (identity and _task_identity(task) == identity):
            task["status"] = "awaiting_approval"
            task["blocked_reason"] = "requires human approval before execution"
            task["blocked_at"] = _now()
            break
    _save_queue(queue)


def _mark_task_contract_validated(task_to_mark: dict[str, Any], contract: TaskContract) -> None:
    queue = _load_queue()
    identity = _task_identity(task_to_mark)
    for task in queue:
        if task == task_to_mark or (identity and _task_identity(task) == identity):
            task["contract_validated_at"] = _now()
            task["contract_version"] = contract.contract_version
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


def _write_launch_record(record: dict[str, Any]) -> bool:
    """Append a launch record atomically and report whether it was persisted."""
    queue = _load_launch_queue()
    # Deduplicate by task_id+status=pending
    existing_ids = {r["task_id"] for r in queue if r.get("status") == "pending"}
    if record["task_id"] in existing_ids:
        log.debug("[Loop] task %s already pending in LAUNCH_QUEUE — skipping", record["task_id"])
        return False
    queue.append(record)
    tmp = str(LAUNCH_QUEUE_PATH) + ".loop.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(queue, f, indent=2)
        os.replace(tmp, str(LAUNCH_QUEUE_PATH))
        return True
    except Exception as exc:
        log.error("[Loop] Could not write LAUNCH_QUEUE.json: %s", exc)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


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

def _git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=10,
        shell=False,
    )
    head = result.stdout.strip()
    if result.returncode != 0 or not head:
        raise RuntimeError(result.stderr.strip() or "git rev-parse HEAD failed")
    return head

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
