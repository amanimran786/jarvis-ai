"""
Jarvis Operative Agent — autonomous multi-step task execution.

The operative takes a high-level goal, breaks it into steps using Claude,
executes each step using Jarvis's tool suite, and reports progress.

Example tasks:
  "Research the best Python async frameworks, write a report, and save it"
  "Check my emails, summarize the urgent ones, and create a to-do note"
  "Search for news about AI safety, write a briefing, and email it to me"
  "Find top 5 Python repos for ML, save the list, and open VS Code"

Usage:
  from operative import run_task
  result = run_task("research X and save a report", on_progress=callback)
"""

import datetime
import fcntl
import hashlib
import logging
import threading
import time
import uuid
from typing import Callable, Iterable

log = logging.getLogger(__name__)

from brains.brain_claude import ask_claude
from config import (
    DEFAULT_MODE,
    HAIKU,
    LOCAL_DEFAULT,
    OPERATIVE_MAX_RECOVERY_ATTEMPTS,
    OPERATIVE_MAX_STEPS,
    OPERATIVE_TIMEOUT_SECONDS,
    VOICE_ENABLED,
)
from harness.tts import speak_step
from harness.audit import set_run_id
import runtime_state
from task_planner import TaskStep as Step, plan_task, replan_after_failure
from execution_engine import (
    capabilities_for_task,
    execute_step,
    execution_capability_scope,
    sensitive_step_numbers,
    task_without_capability_directive,
)
import preflect


_REPLAN_MIN_REMAINING_SECONDS = 95.0
_SUMMARY_MIN_REMAINING_SECONDS = 95.0
_RESUME_LOCKS_GUARD = threading.Lock()
_RESUME_LOCKS: dict[str, threading.Lock] = {}


def _try_acquire_process_run_lock(run_id: str):
    lock_dir = runtime_state.app_data_dir() / "runtime" / "operative_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_name = hashlib.sha256(run_id.encode("utf-8")).hexdigest() + ".lock"
    handle = (lock_dir / lock_name).open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _release_process_run_lock(handle) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


# ── Persistence helpers ────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _persist_task_start(
    run_id: str,
    task: str,
    steps: "list[Step]",
    *,
    capabilities: frozenset[str] = frozenset(),
    budget: dict | None = None,
    created_at: str = "",
) -> bool:
    try:
        import task_persistence as _tp
        return bool(_tp.upsert_task({
            "id": run_id,
            "status": "running",
            "task": task,
            "created_at": created_at or _now_iso(),
            "updated_at": _now_iso(),
            "finished_at": "",
            "steps_total": len(steps),
            "steps_done": 0,
            "plan": [
                {
                    "number": s.number,
                    "description": s.description,
                    "tool": getattr(s, "tool", "chat"),
                    "params": getattr(s, "params", {}),
                }
                for s in steps
            ],
            "result": "",
            "authorized_capabilities": sorted(capabilities),
            "execution_budget": dict(budget or {}),
        }))
    except Exception:
        log.debug("task_persistence unavailable — skipping checkpoint", exc_info=True)
        return False


def _checkpoint_step(run_id: str, step: Step) -> bool:
    try:
        import task_persistence as _tp
        return bool(_tp.checkpoint_step(
            run_id=run_id,
            step_number=step.number,
            description=step.description,
            tool=getattr(step, "tool", "chat"),
            ok=step.ok,
            result=(
                "[SENSITIVE RESULT REDACTED]"
                if step.number in sensitive_step_numbers()
                else step.result
            ),
        ))
    except Exception:
        log.debug("checkpoint_step failed", exc_info=True)
        return False


def _persist_task_finish(
    run_id: str,
    task: str,
    steps: "list[Step]",
    summary: str,
    ok: bool,
    *,
    capabilities: frozenset[str] = frozenset(),
    budget: dict | None = None,
    created_at: str = "",
) -> bool:
    try:
        import task_persistence as _tp
        completed = [s for s in steps if s.ok]
        return bool(_tp.upsert_task({
            "id": run_id,
            "status": "succeeded" if ok else "failed",
            "task": task,
            "created_at": created_at or _now_iso(),
            "updated_at": _now_iso(),
            "finished_at": _now_iso(),
            "steps_total": len(steps),
            "steps_done": len(completed),
            "plan": [
                {
                    "number": s.number,
                    "description": s.description,
                    "tool": getattr(s, "tool", "chat"),
                    "params": getattr(s, "params", {}),
                }
                for s in steps
            ],
            "result": summary[:500],
            "authorized_capabilities": sorted(capabilities),
            "execution_budget": dict(budget or {}),
        }))
    except Exception:
        log.debug("persist_task_finish failed", exc_info=True)
        return False


def _summarize(prompt: str, system_extra: str = "") -> str:
    """Summarize task results. Uses local model unless in cloud-only mode."""
    if DEFAULT_MODE != "cloud":
        try:
            import ollama as _ollama_lib
            from brains.brain_ollama import get_best_available
            try:
                import httpx
                client = _ollama_lib.Client(
                    timeout=httpx.Timeout(connect=10.0, read=90.0, write=15.0, pool=10.0)
                )
            except ImportError:
                client = _ollama_lib.Client(timeout=90.0)
            model = get_best_available(LOCAL_DEFAULT)
            messages = [{"role": "user", "content": prompt}]
            if system_extra:
                messages = [{"role": "system", "content": system_extra}] + messages
            response = client.chat(model=model, messages=messages, stream=False)
            return (response.message.content or "").strip()
        except Exception:
            pass
        # Graceful local fallback — no model call needed
        return prompt.split("\nTask: ", 1)[-1].split("\n")[0].strip() or "Task complete."
    return ask_claude(prompt, model=HAIKU, system_extra=system_extra or None)


def _append_corrective_steps(steps: list[Step], corrective: list[Step]) -> tuple[int, bool]:
    """Append bounded recovery steps with unique monotonically increasing numbers."""
    remaining = max(0, OPERATIVE_MAX_STEPS - len(steps))
    selected = list(corrective)[:remaining]
    next_number = max((step.number for step in steps), default=0) + 1
    for offset, step in enumerate(selected):
        step.number = next_number + offset
    steps.extend(selected)
    return len(selected), len(corrective) > remaining


def _budget_snapshot(
    executed_steps: int,
    recovery_attempts: int,
    elapsed_seconds: float,
    *,
    in_flight_step: Step | None = None,
    sensitive_steps: Iterable[int] | None = None,
) -> dict:
    if sensitive_steps is None:
        sensitive_steps = sensitive_step_numbers()
    budget = {
        "executed_steps": max(0, int(executed_steps)),
        "recovery_attempts": max(0, int(recovery_attempts)),
        "elapsed_seconds": max(0.0, float(elapsed_seconds)),
        "sensitive_step_numbers": sorted({int(number) for number in sensitive_steps}),
    }
    if in_flight_step is not None:
        budget["in_flight_step"] = {
            "number": in_flight_step.number,
            "description": in_flight_step.description,
            "tool": getattr(in_flight_step, "tool", "chat"),
            "recorded_at": _now_iso(),
        }
    return budget


def _deterministic_summary(task: str, completed: list[Step], stop_reason: str) -> str:
    if completed:
        prefix = f"Completed {len(completed)} step(s) for: {task}"
    else:
        prefix = f"No steps completed for: {task}"
    if stop_reason:
        return f"{prefix}. Execution stopped: {stop_reason}."
    return f"{prefix}."


# ── Step definition ───────────────────────────────────────────────────────────


# ── Main entry point ──────────────────────────────────────────────────────────

def run_task(
    task: str,
    on_progress: Callable[[str, str], None] | None = None,
    cancel_event: threading.Event | None = None,
    *,
    authorized_capabilities: Iterable[str] | None = None,
) -> dict:
    """Execute one task under a process-wide run lease."""
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    process_lock = _try_acquire_process_run_lock(run_id)
    if process_lock is None:
        return {
            "task": task,
            "steps": [],
            "summary": "Could not acquire the task execution lease.",
            "results": {},
            "ok": False,
            "stop_reason": "execution_lease_unavailable",
        }
    try:
        return _run_task_locked(
            run_id,
            task,
            on_progress=on_progress,
            cancel_event=cancel_event,
            authorized_capabilities=authorized_capabilities,
        )
    finally:
        _release_process_run_lock(process_lock)


def _run_task_locked(
    run_id: str,
    task: str,
    on_progress: Callable[[str, str], None] | None = None,
    cancel_event: threading.Event | None = None,
    *,
    authorized_capabilities: Iterable[str] | None = None,
) -> dict:
    """
    Execute a multi-step task autonomously.

    Args:
        task:        Natural language description of what to do
        on_progress: Optional callback(step_description, result_preview)

    Returns:
        {
          "task":    original task,
          "steps":   list of Step objects,
          "summary": final summary,
          "ok":      bool,
        }
    """

    def _prog(msg, detail="", *, announce_step: Step | None = None):
        print(f"[Operative] {msg}" + (f": {detail[:100]}" if detail else ""))
        if on_progress:
            on_progress(msg, detail)
        if VOICE_ENABLED and announce_step is not None:
            speak_step(
                announce_step.number,
                announce_step.description,
                ok=announce_step.ok,
            )

    run_created_at = _now_iso()
    set_run_id(run_id)   # thread-local: all audit_log() calls in this thread carry run_id
    run_started = time.monotonic()
    deadline = run_started + OPERATIVE_TIMEOUT_SECONDS
    capabilities = capabilities_for_task(
        task,
        trusted_capabilities=authorized_capabilities,
    )
    planner_task = task_without_capability_directive(task)

    _prog("Planning task", planner_task)
    planned_steps = list(plan_task(planner_task))
    plan_truncated = len(planned_steps) > OPERATIVE_MAX_STEPS
    steps = planned_steps[:OPERATIVE_MAX_STEPS]
    _prog(f"Plan ready — {len(steps)} steps",
          " → ".join(s.description for s in steps))

    if preflect.is_enabled():
        pf = preflect.review_plan(planner_task, steps, task_id=run_id)
        if pf.fired:
            _prog("Plan pre-checked", f"{pf.verdict}: {pf.summary}")

    step_results: dict[int, str] = {}
    executed_steps = 0
    recovery_attempts = 0
    stop_reason = "time_limit" if time.monotonic() >= deadline else ""
    uncertain_execution = False
    persistence_ok = _persist_task_start(
        run_id,
        task,
        steps,
        capabilities=capabilities,
        budget=_budget_snapshot(
            executed_steps,
            recovery_attempts,
            time.monotonic() - run_started,
        ),
        created_at=run_created_at,
    )
    if not persistence_ok:
        stop_reason = "task_persistence_failed"
        _prog("Task state could not be persisted", "execution blocked")

    persisted_sensitive_steps: set[int] = set()
    with execution_capability_scope(capabilities, deadline=deadline):
        for step in steps:
            if not persistence_ok or stop_reason == "time_limit":
                break
            if cancel_event and cancel_event.is_set():
                stop_reason = "cancelled"
                _prog("Task cancelled", "stopping before step")
                break
            if executed_steps >= OPERATIVE_MAX_STEPS:
                stop_reason = "step_limit"
                _prog("Task step limit reached", str(OPERATIVE_MAX_STEPS))
                break
            if time.monotonic() >= deadline:
                stop_reason = "time_limit"
                _prog("Task time limit reached", f"{OPERATIVE_TIMEOUT_SECONDS}s")
                break

            intent_budget = _budget_snapshot(
                executed_steps,
                recovery_attempts,
                time.monotonic() - run_started,
                in_flight_step=step,
            )
            persistence_ok = _persist_task_start(
                run_id,
                task,
                steps,
                capabilities=capabilities,
                budget=intent_budget,
                created_at=run_created_at,
            )
            if not persistence_ok:
                stop_reason = "execution_intent_persistence_failed"
                _prog("Step intent could not be persisted", "execution blocked")
                break

            _prog(f"Step {step.number}: {step.description}")
            ok, result = execute_step(step, step_results, run_id=run_id)
            executed_steps += 1
            step.ok = ok
            step.result = result
            step_results[step.number] = result
            persisted_sensitive_steps = set(sensitive_step_numbers())

            if not _checkpoint_step(run_id, step):
                stop_reason = "step_checkpoint_failed"
                uncertain_execution = True
                _prog("Step result checkpoint failed", "automatic replay disabled")
                break

            persistence_ok = _persist_task_start(
                run_id,
                task,
                steps,
                capabilities=capabilities,
                budget=_budget_snapshot(
                    executed_steps,
                    recovery_attempts,
                    time.monotonic() - run_started,
                ),
                created_at=run_created_at,
            )
            if not persistence_ok:
                stop_reason = "budget_checkpoint_failed"
                uncertain_execution = True

            preview = result[:120].replace("\n", " ")
            status = "✓" if ok else "✗"
            _prog(f"  {status} {step.description}", preview, announce_step=step)

            if not persistence_ok:
                _prog("Task budget checkpoint failed", "automatic replay disabled")
                break

            if time.monotonic() >= deadline:
                stop_reason = "time_limit"
                _prog("Task time limit reached", f"{OPERATIVE_TIMEOUT_SECONDS}s")
                break

            if not ok:
                if deadline - time.monotonic() < _REPLAN_MIN_REMAINING_SECONDS:
                    stop_reason = "time_limit"
                    _prog("Insufficient time for recovery", f"{OPERATIVE_TIMEOUT_SECONDS}s budget")
                    break
                if recovery_attempts >= OPERATIVE_MAX_RECOVERY_ATTEMPTS:
                    _prog("Recovery budget exhausted", str(OPERATIVE_MAX_RECOVERY_ATTEMPTS))
                    continue
                recovery_attempts += 1
                persistence_ok = _persist_task_start(
                    run_id,
                    task,
                    steps,
                    capabilities=capabilities,
                    budget=_budget_snapshot(
                        executed_steps,
                        recovery_attempts,
                        time.monotonic() - run_started,
                    ),
                    created_at=run_created_at,
                )
                if not persistence_ok:
                    stop_reason = "recovery_checkpoint_failed"
                    _prog("Recovery budget could not be persisted", "replanning blocked")
                    break
                _prog(f"Step {step.number} failed — attempting recovery", result)
                corrective = replan_after_failure(
                    planner_task,
                    completed_steps=[s for s in steps if s.ok],
                    failed_step=step,
                    error=(
                        "Sensitive step failed; output withheld from recovery planner."
                        if step.number in persisted_sensitive_steps
                        else result
                    ),
                )
                if corrective:
                    added, truncated = _append_corrective_steps(steps, corrective)
                    if truncated:
                        stop_reason = "step_limit"
                    if added:
                        persistence_ok = _persist_task_start(
                            run_id,
                            task,
                            steps,
                            capabilities=capabilities,
                            budget=_budget_snapshot(
                                executed_steps,
                                recovery_attempts,
                                time.monotonic() - run_started,
                            ),
                            created_at=run_created_at,
                        )
                        if not persistence_ok:
                            stop_reason = "recovery_plan_persistence_failed"
                        _prog(
                            f"Recovery plan: {added} corrective step(s)",
                            " → ".join(s.description for s in steps[-added:]),
                        )
                        if not persistence_ok:
                            break

    if plan_truncated and not stop_reason:
        stop_reason = "step_limit"

    # Final summary
    completed = [s for s in steps if s.ok]
    failed    = [s for s in steps if not s.ok]

    summary_prompt = (
        f"Summarize what was accomplished in this task in 2-3 spoken sentences.\n"
        f"Task: {planner_task}\n"
        f"Steps completed: {[s.description for s in completed]}\n"
        f"Final output preview: {step_results.get(max(step_results.keys(), default=0), '')[:500]}"
    )
    system_extra = ""
    technical = False
    try:
        import model_router
        technical = model_router._is_engineering_companion_query(planner_task, "chat")
        if technical:
            system_extra = model_router._engineering_companion_grounding(planner_task)
    except Exception:
        system_extra = ""
    if technical:
        summary_prompt = (
            "Summarize what was accomplished in this task in 2-3 spoken sentences. "
            "Lead with the conclusion or fix first. "
            "Then name the key tradeoff, root cause, or next verification step.\n"
            f"Task: {planner_task}\n"
            f"Steps completed: {[s.description for s in completed]}\n"
            f"Final output preview: {step_results.get(max(step_results.keys(), default=0), '')[:500]}"
        )
    sensitive_cloud_summary = DEFAULT_MODE == "cloud" and bool(persisted_sensitive_steps)
    if (
        uncertain_execution
        or sensitive_cloud_summary
        or deadline - time.monotonic() < _SUMMARY_MIN_REMAINING_SECONDS
    ):
        summary = _deterministic_summary(planner_task, completed, stop_reason)
    else:
        summary = _summarize(summary_prompt, system_extra=system_extra)

    task_ok = len(failed) == 0 and not stop_reason
    final_budget = _budget_snapshot(
        executed_steps,
        recovery_attempts,
        time.monotonic() - run_started,
        sensitive_steps=persisted_sensitive_steps,
    )
    if not uncertain_execution:
        finish_ok = _persist_task_finish(
            run_id,
            task,
            steps,
            summary,
            task_ok,
            capabilities=capabilities,
            budget=final_budget,
            created_at=run_created_at,
        )
        if not finish_ok:
            stop_reason = stop_reason or "final_persistence_failed"
            task_ok = False
    _prog("Task complete", summary[:100])
    set_run_id("")  # clear thread-local so a reused thread doesn't leak run_id

    return {
        "task":    task,
        "steps":   steps,
        "summary": summary,
        "results": step_results,
        "ok":      task_ok,
        "stop_reason": stop_reason,
        "budget": {
            "executed_steps": executed_steps,
            "recovery_attempts": recovery_attempts,
            "max_steps": OPERATIVE_MAX_STEPS,
            "max_recovery_attempts": OPERATIVE_MAX_RECOVERY_ATTEMPTS,
            "timeout_seconds": OPERATIVE_TIMEOUT_SECONDS,
            "elapsed_seconds": final_budget["elapsed_seconds"],
            "sensitive_step_numbers": final_budget["sensitive_step_numbers"],
        },
        "authorized_capabilities": sorted(capabilities),
    }


def resume_task(
    run_id: str,
    on_progress: Callable[[str, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Resume once per run id; concurrent callers fail closed."""
    with _RESUME_LOCKS_GUARD:
        resume_lock = _RESUME_LOCKS.setdefault(run_id, threading.Lock())
    if not resume_lock.acquire(blocking=False):
        return {
            "task": run_id,
            "steps": [],
            "summary": "Another worker is already resuming this task.",
            "results": {},
            "ok": False,
            "stop_reason": "resume_already_in_progress",
        }
    process_lock = _try_acquire_process_run_lock(run_id)
    if process_lock is None:
        resume_lock.release()
        return {
            "task": run_id,
            "steps": [],
            "summary": "The task is still active in another Jarvis process.",
            "results": {},
            "ok": False,
            "stop_reason": "resume_already_in_progress",
        }
    try:
        return _resume_task_locked(
            run_id,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
    finally:
        _release_process_run_lock(process_lock)
        resume_lock.release()


def _resume_task_locked(
    run_id: str,
    on_progress: Callable[[str, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Resume an interrupted task from its last checkpoint.

    Loads the stored plan and completed steps from task_persistence, then
    re-executes only the remaining steps.  Returns the same shape as run_task().
    """
    import task_persistence as _tp

    interrupted = _tp.find_interrupted_tasks()
    match = next((t for t in interrupted if t.get("id") == run_id), None)
    if match is None:
        return {
            "task": run_id,
            "steps": [],
            "summary": f"No interrupted task found with run_id={run_id!r}.",
            "results": {},
            "ok": False,
        }

    task: str = match.get("task", "")
    planner_task = task_without_capability_directive(task)
    task_created_at = str(match.get("created_at") or _now_iso())
    raw_plan: list = match.get("plan", [])
    step_events: list = match.get("step_events", [])

    # Reconstruct Step objects from stored plan.
    reconstructed_steps = [
        Step(
            number=p["number"],
            description=p["description"],
            tool=p.get("tool", "chat"),
            params=p.get("params", {}),
        )
        for p in raw_plan
    ]
    plan_truncated = len(reconstructed_steps) > OPERATIVE_MAX_STEPS
    steps = reconstructed_steps[:OPERATIVE_MAX_STEPS]
    stored_capabilities = frozenset(
        str(item).strip().lower().replace("-", "_")
        for item in (match.get("authorized_capabilities") or [])
        if str(item).strip()
    )
    capabilities = capabilities_for_task(
        task,
        trusted_capabilities=stored_capabilities,
    )
    if capabilities != stored_capabilities:
        return {
            "task": task,
            "steps": steps,
            "summary": "The persisted capability grant no longer matches the original task.",
            "results": {},
            "ok": False,
            "stop_reason": "capability_grant_mismatch",
            "authorized_capabilities": sorted(capabilities),
        }

    stored_budget = match.get("execution_budget") or {}
    in_flight_step = stored_budget.get("in_flight_step")
    if in_flight_step:
        return {
            "task": task,
            "steps": steps,
            "summary": (
                "The previous process stopped while a step was in flight. "
                "Automatic replay is disabled; inspect the persisted task before continuing."
            ),
            "results": {},
            "ok": False,
            "stop_reason": "uncertain_in_flight_step",
            "budget": dict(stored_budget),
            "authorized_capabilities": sorted(capabilities),
        }

    # Inject already-completed results so downstream $step_N_result refs resolve.
    step_results: dict[int, str] = {}
    done_numbers: set[int] = set()
    attempted_numbers: set[int] = set()
    for ev in step_events:
        n = ev.get("step_number")
        if n is not None:
            step_results[n] = ev.get("result", "")
            attempted_numbers.add(n)
            if ev.get("ok"):
                done_numbers.add(n)
            # Mark step objects that are already done.
            for s in steps:
                if s.number == n:
                    s.ok = ev.get("ok", False)
                    s.result = ev.get("result", "")

    def _prog(msg: str, detail: str = "", *, announce_step: Step | None = None) -> None:
        print(f"[Operative/resume] {msg}" + (f": {detail[:100]}" if detail else ""))
        if on_progress:
            on_progress(msg, detail)
        if VOICE_ENABLED and announce_step is not None:
            speak_step(announce_step.number, announce_step.description, ok=announce_step.ok)

    already_done = len(done_numbers)
    _prog(f"Resuming '{task[:60]}'", f"{already_done}/{len(steps)} steps already done")

    executed_steps = max(
        len(attempted_numbers),
        int(stored_budget.get("executed_steps", 0) or 0),
    )
    recovery_attempts = max(0, int(stored_budget.get("recovery_attempts", 0) or 0))
    prior_elapsed = max(0.0, float(stored_budget.get("elapsed_seconds", 0.0) or 0.0))
    persisted_sensitive_steps = {
        int(number) for number in stored_budget.get("sensitive_step_numbers", [])
    }
    run_started = time.monotonic()
    deadline = run_started + max(0.0, OPERATIVE_TIMEOUT_SECONDS - prior_elapsed)
    stop_reason = "time_limit" if time.monotonic() >= deadline else ""
    uncertain_execution = False
    persistence_ok = _persist_task_start(
        run_id,
        task,
        steps,
        capabilities=capabilities,
        budget=_budget_snapshot(
            executed_steps,
            recovery_attempts,
            prior_elapsed,
            sensitive_steps=persisted_sensitive_steps,
        ),
        created_at=task_created_at,
    )
    if not persistence_ok:
        stop_reason = "task_persistence_failed"
        _prog("Task state could not be persisted", "execution blocked")

    with execution_capability_scope(
        capabilities,
        deadline=deadline,
        sensitive_step_numbers=persisted_sensitive_steps,
        unavailable_step_numbers=persisted_sensitive_steps,
    ):
        for step in steps:
            if step.number in attempted_numbers:
                status = "✓" if step.number in done_numbers else "✗"
                _prog(f"  {status} (skip attempted) Step {step.number}: {step.description}")
                continue
            if not persistence_ok or stop_reason == "time_limit":
                break
            if cancel_event and cancel_event.is_set():
                stop_reason = "cancelled"
                _prog("Task cancelled during resume")
                break
            if executed_steps >= OPERATIVE_MAX_STEPS:
                stop_reason = "step_limit"
                _prog("Task step limit reached during resume", str(OPERATIVE_MAX_STEPS))
                break
            if time.monotonic() >= deadline:
                stop_reason = "time_limit"
                _prog("Task time limit reached during resume", f"{OPERATIVE_TIMEOUT_SECONDS}s")
                break

            intent_budget = _budget_snapshot(
                executed_steps,
                recovery_attempts,
                prior_elapsed + max(0.0, time.monotonic() - run_started),
                in_flight_step=step,
            )
            persistence_ok = _persist_task_start(
                run_id,
                task,
                steps,
                capabilities=capabilities,
                budget=intent_budget,
                created_at=task_created_at,
            )
            if not persistence_ok:
                stop_reason = "execution_intent_persistence_failed"
                _prog("Step intent could not be persisted", "execution blocked")
                break

            _prog(f"Step {step.number}: {step.description}")
            ok, result = execute_step(step, step_results, run_id=run_id)
            executed_steps += 1
            step.ok = ok
            step.result = result
            step_results[step.number] = result
            attempted_numbers.add(step.number)
            persisted_sensitive_steps = set(sensitive_step_numbers())
            if not _checkpoint_step(run_id, step):
                stop_reason = "step_checkpoint_failed"
                uncertain_execution = True
                _prog("Step result checkpoint failed", "automatic replay disabled")
                break

            persistence_ok = _persist_task_start(
                run_id,
                task,
                steps,
                capabilities=capabilities,
                budget=_budget_snapshot(
                    executed_steps,
                    recovery_attempts,
                    prior_elapsed + max(0.0, time.monotonic() - run_started),
                ),
                created_at=task_created_at,
            )
            if not persistence_ok:
                stop_reason = "budget_checkpoint_failed"
                uncertain_execution = True

            preview = result[:120].replace("\n", " ")
            status = "✓" if ok else "✗"
            _prog(f"  {status} {step.description}", preview, announce_step=step)

            if not persistence_ok:
                _prog("Task budget checkpoint failed", "automatic replay disabled")
                break

            if time.monotonic() >= deadline:
                stop_reason = "time_limit"
                _prog("Task time limit reached during resume", f"{OPERATIVE_TIMEOUT_SECONDS}s")
                break

            if not ok:
                if deadline - time.monotonic() < _REPLAN_MIN_REMAINING_SECONDS:
                    stop_reason = "time_limit"
                    _prog("Insufficient time for recovery", f"{OPERATIVE_TIMEOUT_SECONDS}s budget")
                    break
                if recovery_attempts >= OPERATIVE_MAX_RECOVERY_ATTEMPTS:
                    _prog("Recovery budget exhausted", str(OPERATIVE_MAX_RECOVERY_ATTEMPTS))
                    continue
                recovery_attempts += 1
                persistence_ok = _persist_task_start(
                    run_id,
                    task,
                    steps,
                    capabilities=capabilities,
                    budget=_budget_snapshot(
                        executed_steps,
                        recovery_attempts,
                        prior_elapsed + max(0.0, time.monotonic() - run_started),
                    ),
                    created_at=task_created_at,
                )
                if not persistence_ok:
                    stop_reason = "recovery_checkpoint_failed"
                    _prog("Recovery budget could not be persisted", "replanning blocked")
                    break
                _prog(f"Step {step.number} failed — attempting recovery", result)
                corrective = replan_after_failure(
                    planner_task,
                    completed_steps=[s for s in steps if s.ok],
                    failed_step=step,
                    error=(
                        "Sensitive step failed; output withheld from recovery planner."
                        if step.number in persisted_sensitive_steps
                        else result
                    ),
                )
                if corrective:
                    added, truncated = _append_corrective_steps(steps, corrective)
                    if truncated:
                        stop_reason = "step_limit"
                    if added:
                        persistence_ok = _persist_task_start(
                            run_id,
                            task,
                            steps,
                            capabilities=capabilities,
                            budget=_budget_snapshot(
                                executed_steps,
                                recovery_attempts,
                                prior_elapsed + max(
                                    0.0,
                                    time.monotonic() - run_started,
                                ),
                            ),
                            created_at=task_created_at,
                        )
                        if not persistence_ok:
                            stop_reason = "recovery_plan_persistence_failed"
                            break

    if plan_truncated and not stop_reason:
        stop_reason = "step_limit"

    completed = [s for s in steps if s.ok]
    failed = [s for s in steps if not s.ok]

    summary_prompt = (
        f"Summarize what was accomplished in this resumed task in 2-3 sentences.\n"
        f"Task: {planner_task}\n"
        f"Steps completed: {[s.description for s in completed]}\n"
        f"Final output preview: {step_results.get(max(step_results.keys(), default=0), '')[:500]}"
    )
    sensitive_cloud_summary = DEFAULT_MODE == "cloud" and bool(persisted_sensitive_steps)
    if (
        uncertain_execution
        or sensitive_cloud_summary
        or deadline - time.monotonic() < _SUMMARY_MIN_REMAINING_SECONDS
    ):
        summary = _deterministic_summary(planner_task, completed, stop_reason)
    else:
        summary = _summarize(summary_prompt)

    task_ok = len(failed) == 0 and not stop_reason
    final_budget = _budget_snapshot(
        executed_steps,
        recovery_attempts,
        prior_elapsed + max(0.0, time.monotonic() - run_started),
        sensitive_steps=persisted_sensitive_steps,
    )
    if not uncertain_execution:
        finish_ok = _persist_task_finish(
            run_id,
            task,
            steps,
            summary,
            task_ok,
            capabilities=capabilities,
            budget=final_budget,
            created_at=task_created_at,
        )
        if not finish_ok:
            stop_reason = stop_reason or "final_persistence_failed"
            task_ok = False
    _prog("Resume complete", summary[:100])

    return {
        "task": task,
        "steps": steps,
        "summary": summary,
        "results": step_results,
        "ok": task_ok,
        "stop_reason": stop_reason,
        "budget": {
            "executed_steps": executed_steps,
            "recovery_attempts": recovery_attempts,
            "max_steps": OPERATIVE_MAX_STEPS,
            "max_recovery_attempts": OPERATIVE_MAX_RECOVERY_ATTEMPTS,
            "timeout_seconds": OPERATIVE_TIMEOUT_SECONDS,
            "elapsed_seconds": final_budget["elapsed_seconds"],
            "sensitive_step_numbers": final_budget["sensitive_step_numbers"],
        },
        "authorized_capabilities": sorted(capabilities),
    }


def run_task_async(
    task: str,
    on_progress: Callable[[str, str], None] | None = None,
    on_complete: Callable[[dict], None] | None = None,
    cancel_event: threading.Event | None = None,
    *,
    authorized_capabilities: Iterable[str] | None = None,
) -> threading.Thread:
    """Run task in background. Calls on_complete(result) when done."""
    def _run():
        result = run_task(
            task,
            on_progress=on_progress,
            cancel_event=cancel_event,
            authorized_capabilities=authorized_capabilities,
        )
        if on_complete:
            on_complete(result)

    t = threading.Thread(target=_run, daemon=True, name="Operative")
    t.start()
    return t
