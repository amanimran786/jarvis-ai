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
    LOCAL_CODER,
    LOCAL_DEFAULT,
    LOCAL_REASONING,
    LOCAL_TUNED,
    OPERATIVE_APPROVAL_TTL_SECONDS,
    OPERATIVE_GRANT_TTL_SECONDS,
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
    execute_step,
    execution_capability_scope,
    sensitive_step_numbers,
)
from operative_approval import (
    ApprovalError,
    ExecutionGrant,
    ExecutionManifest,
    RouteContext,
    approval_summary,
    build_manifest,
    manifest_steps,
    new_approval_id,
    validate_manifest_semantics,
)
import safety_permissions
import preflect


_REPLAN_MIN_REMAINING_SECONDS = 95.0
_SUMMARY_MIN_REMAINING_SECONDS = 95.0
_RESUME_LOCKS_GUARD = threading.Lock()
_RESUME_LOCKS: dict[str, threading.Lock] = {}


def _step_may_have_side_effect(step: Step) -> bool:
    tool = str(getattr(step, "tool", "") or "").strip().lower()
    params = dict(getattr(step, "params", {}) or {})
    action = str(params.get("action") or "").strip().lower()
    if tool == "file":
        return action == "write"
    if tool == "notes":
        return action == "write"
    if tool == "email":
        return action == "send"
    if tool == "git":
        return action in {"add", "add_all", "commit"}
    return tool in {
        "terminal",
        "code_task",
        "specialized_agent",
        "malware_submit_hash",
    }


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
    approval: dict | None = None,
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
            "execution_approval": dict(approval or {}),
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
    approval: dict | None = None,
    created_at: str = "",
    outcome: str = "",
) -> bool:
    try:
        import task_persistence as _tp
        completed = [s for s in steps if s.ok]
        now_iso = _now_iso()
        record = {
            "id": run_id,
            "status": "succeeded" if ok else "failed",
            "task": task,
            "created_at": created_at or _now_iso(),
            "updated_at": now_iso,
            "finished_at": now_iso,
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
            "execution_approval": dict(approval or {}),
        }
        approval_id = str((approval or {}).get("approval_id") or "")
        if approval_id:
            return bool(_tp.terminalize_operative_task(
                record,
                approval_id=approval_id,
                run_id=run_id,
                outcome=outcome or ("succeeded" if ok else "failed"),
                now_iso=now_iso,
            ))
        return bool(_tp.upsert_task(record))
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


def _current_provider_policy() -> dict:
    from local_runtime import local_model_eval
    import model_router
    import provider_router

    policy = provider_router.runtime_policy()
    return {
        "mode": model_router.get_mode(),
        "models": {
            "local_default": LOCAL_DEFAULT,
            "local_reasoning": LOCAL_REASONING,
            "local_coder": LOCAL_CODER,
            "local_tuned": LOCAL_TUNED,
            "promoted": local_model_eval.promoted_model(),
        },
        "free_first_enabled": bool(policy.get("free_first_enabled")),
        "paid_fallback_enabled": bool(policy.get("paid_fallback_enabled")),
        "provider_priority": policy.get("provider_priority", {}),
    }


def _execution_budget_contract() -> dict:
    return {
        "max_steps": OPERATIVE_MAX_STEPS,
        "max_recovery_attempts": OPERATIVE_MAX_RECOVERY_ATTEMPTS,
        "timeout_seconds": OPERATIVE_TIMEOUT_SECONDS,
    }


def _approval_metadata(
    manifest: ExecutionManifest,
    *,
    approval_id: str = "",
    grant_expires_at: str = "",
) -> dict:
    return {
        "approval_id": approval_id,
        "manifest_digest": manifest.digest,
        "principal": manifest.principal,
        "session_id": manifest.session_id,
        "source": manifest.source,
        "grant_expires_at": grant_expires_at,
    }


def _manifest_matches_runtime(manifest: ExecutionManifest) -> bool:
    return (
        manifest.budget == _execution_budget_contract()
        and manifest.provider_policy == _current_provider_policy()
    )


def prepare_task(
    task: str,
    *,
    context: RouteContext | None = None,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Plan once and persist an exact proposal when privileged access is needed."""
    started_at = time.monotonic()
    route_context = (context or RouteContext.desktop()).normalized()
    task_text = str(task or "").strip()
    if cancel_event is not None and cancel_event.is_set():
        raise ApprovalError("Task preparation was cancelled.")
    planned_steps = list(plan_task(task_text))[:OPERATIVE_MAX_STEPS]
    if cancel_event is not None and cancel_event.is_set():
        raise ApprovalError("Task preparation was cancelled.")
    if time.monotonic() - started_at >= OPERATIVE_TIMEOUT_SECONDS:
        raise ApprovalError("Task preparation exceeded the operative timeout.")
    manifest = build_manifest(
        task_text,
        planned_steps,
        context=route_context,
        budget=_execution_budget_contract(),
        provider_policy=_current_provider_policy(),
        approval_ttl_seconds=OPERATIVE_APPROVAL_TTL_SECONDS,
    )
    if not manifest.capabilities:
        return {
            "status": "ready",
            "manifest": manifest.to_dict(),
            "manifest_digest": manifest.digest,
            "summary": "The prepared plan requires no privileged capabilities.",
        }

    import task_persistence as _tp

    approval_id = new_approval_id()
    record = {
        "approval_id": approval_id,
        "manifest_digest": manifest.digest,
        "principal": manifest.principal,
        "session_id": manifest.session_id,
        "source": manifest.source,
        "created_at": manifest.created_at,
        "expires_at": manifest.expires_at,
        "manifest": manifest.to_dict(),
    }
    if not _tp.create_operative_proposal(record):
        raise ApprovalError("The execution proposal could not be persisted.")
    return {
        "status": "approval_required",
        "approval_id": approval_id,
        "manifest_digest": manifest.digest,
        "capabilities": list(manifest.capabilities),
        "resources": manifest.resources,
        "expires_at": manifest.expires_at,
        "summary": approval_summary(manifest, approval_id),
    }


def execute_prepared_task(
    manifest_value: dict,
    on_progress: Callable[[str, str], None] | None = None,
    cancel_event: threading.Event | None = None,
    *,
    context: RouteContext | None = None,
) -> dict:
    """Execute a capability-free prepared manifest without planning again."""
    manifest = ExecutionManifest.from_dict(manifest_value)
    route_context = (context or RouteContext.desktop()).normalized()
    if (
        manifest.principal != route_context.principal
        or manifest.session_id != route_context.session_id
        or manifest.source != route_context.source
    ):
        return _approval_failure(manifest.task, "approval_context_mismatch")
    if manifest.capabilities:
        return _approval_failure(manifest.task, "approval_required")
    if manifest.is_expired() or not _manifest_matches_runtime(manifest):
        return _approval_failure(manifest.task, "prepared_manifest_expired_or_changed")
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    process_lock = _try_acquire_process_run_lock(run_id)
    if process_lock is None:
        return _approval_failure(manifest.task, "execution_lease_unavailable")
    try:
        return _run_task_locked(
            run_id,
            manifest.task,
            on_progress=on_progress,
            cancel_event=cancel_event,
            prepared_steps=manifest_steps(manifest),
            approval=_approval_metadata(manifest),
            provider_policy=manifest.provider_policy,
        )
    finally:
        _release_process_run_lock(process_lock)


def approve_and_run_task(
    approval_id: str,
    on_progress: Callable[[str, str], None] | None = None,
    cancel_event: threading.Event | None = None,
    *,
    context: RouteContext | None = None,
) -> dict:
    """Approve and atomically consume one exact proposal before execution."""
    import task_persistence as _tp

    route_context = (context or RouteContext.desktop()).normalized()
    pending = _tp.get_operative_proposal(approval_id)
    if not pending:
        return _approval_failure(str(approval_id), "approval_not_found")
    try:
        manifest = ExecutionManifest.from_dict(pending["manifest"])
    except (KeyError, ApprovalError):
        return _approval_failure(str(approval_id), "approval_manifest_invalid")
    if (
        pending.get("manifest_digest") != manifest.digest
        or not validate_manifest_semantics(manifest)
    ):
        return _approval_failure(manifest.task, "approval_manifest_changed")
    if (
        manifest.principal != route_context.principal
        or manifest.session_id != route_context.session_id
        or manifest.source != route_context.source
        or not route_context.authenticated
    ):
        return _approval_failure(manifest.task, "approval_context_mismatch")
    if manifest.is_expired() or not _manifest_matches_runtime(manifest):
        return _approval_failure(manifest.task, "approval_expired_or_runtime_changed")

    now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.isoformat()
    grant_expires_at = (
        now + datetime.timedelta(seconds=OPERATIVE_GRANT_TTL_SECONDS)
    ).isoformat()
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    process_lock = _try_acquire_process_run_lock(run_id)
    if process_lock is None:
        return _approval_failure(manifest.task, "execution_lease_unavailable")
    try:
        approval = _approval_metadata(
            manifest,
            approval_id=approval_id,
            grant_expires_at=grant_expires_at,
        )
        task_record = {
            "id": run_id,
            "status": "running",
            "task": manifest.task,
            "created_at": now_iso,
            "updated_at": now_iso,
            "finished_at": "",
            "steps_total": len(manifest.plan),
            "steps_done": 0,
            "plan": manifest.plan,
            "result": "",
            "authorized_capabilities": list(manifest.capabilities),
            "execution_budget": _budget_snapshot(0, 0, 0.0),
            "execution_approval": approval,
        }
        consumed = _tp.consume_operative_approval(
            approval_id,
            manifest_digest=manifest.digest,
            principal=route_context.principal,
            session_id=route_context.session_id,
            source=route_context.source,
            run_id=run_id,
            now_iso=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            grant_expires_at=grant_expires_at,
            task_record=task_record,
        )
        if not consumed:
            latest = _tp.get_operative_proposal(approval_id) or {}
            status = str(latest.get("status") or "")
            reason = {
                "cancelled": "approval_cancelled",
                "consumed": "approval_already_consumed",
            }.get(status, "approval_not_pending")
            return _approval_failure(manifest.task, reason)
        grant = ExecutionGrant(
            approval_id=approval_id,
            manifest_digest=manifest.digest,
            principal=manifest.principal,
            session_id=manifest.session_id,
            source=manifest.source,
            run_id=run_id,
            grant_expires_at=str(consumed.get("grant_expires_at") or ""),
            capabilities=manifest.capabilities,
            resources_json=manifest.resources_json,
        )
        result = _run_task_locked(
            run_id,
            manifest.task,
            on_progress=on_progress,
            cancel_event=cancel_event,
            prepared_steps=manifest_steps(manifest),
            execution_grant=grant,
            approval=approval,
            provider_policy=manifest.provider_policy,
        )
        return result
    finally:
        _release_process_run_lock(process_lock)


def cancel_task_approval(
    approval_id: str,
    *,
    context: RouteContext | None = None,
) -> bool:
    import task_persistence as _tp

    route_context = (context or RouteContext.desktop()).normalized()
    return _tp.cancel_operative_proposal(
        approval_id,
        principal=route_context.principal,
        session_id=route_context.session_id,
        source=route_context.source,
        now_iso=_now_iso(),
    )


def _approval_failure(task: str, stop_reason: str) -> dict:
    return {
        "task": task,
        "steps": [],
        "summary": f"Task execution blocked: {stop_reason.replace('_', ' ')}.",
        "results": {},
        "ok": False,
        "stop_reason": stop_reason,
        "authorized_capabilities": [],
    }


def _create_recovery_proposal(
    task: str,
    corrective_steps: list[Step],
    grant: ExecutionGrant,
) -> dict | None:
    import task_persistence as _tp

    if not corrective_steps:
        return None
    context = RouteContext(
        principal=grant.principal,
        session_id=grant.session_id,
        source=grant.source,
        authenticated=True,
    )
    try:
        manifest = build_manifest(
            f"Recovery for: {task}",
            corrective_steps,
            context=context,
            budget=_execution_budget_contract(),
            provider_policy=_current_provider_policy(),
            approval_ttl_seconds=OPERATIVE_APPROVAL_TTL_SECONDS,
        )
    except ApprovalError:
        log.exception("recovery plan could not be represented by an approval manifest")
        return None
    approval_id = new_approval_id()
    if not _tp.create_operative_proposal(
        {
            "approval_id": approval_id,
            "manifest_digest": manifest.digest,
            "principal": manifest.principal,
            "session_id": manifest.session_id,
            "source": manifest.source,
            "created_at": manifest.created_at,
            "expires_at": manifest.expires_at,
            "manifest": manifest.to_dict(),
        }
    ):
        return None
    return {
        "approval_id": approval_id,
        "manifest_digest": manifest.digest,
        "summary": approval_summary(manifest, approval_id),
    }


# ── Step definition ───────────────────────────────────────────────────────────


# ── Main entry point ──────────────────────────────────────────────────────────

def run_task(
    task: str,
    on_progress: Callable[[str, str], None] | None = None,
    cancel_event: threading.Event | None = None,
    authorized_capabilities: Iterable[str] | None = None,
) -> dict:
    """Execute a capability-free task; privileged plans use the approval flow."""
    if authorized_capabilities is not None:
        return _approval_failure(task, "explicit_approval_required_for_capabilities")
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
        )
    finally:
        _release_process_run_lock(process_lock)


def _run_task_locked(
    run_id: str,
    task: str,
    on_progress: Callable[[str, str], None] | None = None,
    cancel_event: threading.Event | None = None,
    *,
    prepared_steps: list[Step] | None = None,
    execution_grant: ExecutionGrant | None = None,
    approval: dict | None = None,
    provider_policy: dict | None = None,
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
    capabilities = frozenset(execution_grant.capabilities if execution_grant else ())
    planner_task = str(task or "").strip()

    if prepared_steps is None:
        _prog("Planning task", planner_task)
        planned_steps = list(plan_task(planner_task))
    else:
        planned_steps = list(prepared_steps)
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
    reapproval: dict | None = None
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
        approval=approval,
        created_at=run_created_at,
    )
    if not persistence_ok:
        stop_reason = "task_persistence_failed"
        _prog("Task state could not be persisted", "execution blocked")

    persisted_sensitive_steps: set[int] = set()
    grant_scope = execution_grant.to_scope() if execution_grant else None
    with execution_capability_scope(
        capabilities,
        deadline=deadline,
        require_resource_grant=execution_grant is not None,
        provider_policy=provider_policy,
    ), safety_permissions.execution_grant_scope(grant_scope):
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
                approval=approval,
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
                approval=approval,
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
                if execution_grant is not None:
                    if _step_may_have_side_effect(step):
                        stop_reason = "uncertain_side_effect_outcome"
                        _prog(
                            "Side-effect outcome requires reconciliation",
                            "automatic recovery is blocked",
                        )
                        break
                    stop_reason = "reapproval_required"
                    corrective = None
                    if deadline - time.monotonic() >= _REPLAN_MIN_REMAINING_SECONDS:
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
                    reapproval = _create_recovery_proposal(
                        planner_task,
                        list(corrective or []),
                        execution_grant,
                    )
                    _prog(
                        "Recovery requires a new approval",
                        (
                            "approval proposal created"
                            if reapproval
                            else "submit a new task after reconciling the failed action"
                        ),
                    )
                    break
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
                    approval=approval,
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
                            approval=approval,
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
    if reapproval:
        summary = f"{summary} {reapproval['summary']}"

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
            approval=approval,
            created_at=run_created_at,
            outcome=stop_reason or ("succeeded" if task_ok else "failed"),
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
        "execution_approval": dict(approval or {}),
        "reapproval": dict(reapproval or {}),
    }


def resume_task(
    run_id: str,
    on_progress: Callable[[str, str], None] | None = None,
    cancel_event: threading.Event | None = None,
    *,
    context: RouteContext | None = None,
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
            context=context,
        )
    finally:
        _release_process_run_lock(process_lock)
        resume_lock.release()


def _resume_task_locked(
    run_id: str,
    on_progress: Callable[[str, str], None] | None = None,
    cancel_event: threading.Event | None = None,
    *,
    context: RouteContext | None = None,
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
    planner_task = str(task or "").strip()
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
    approval = dict(match.get("execution_approval") or {})
    route_context = (context or RouteContext.desktop()).normalized()
    if approval:
        if (
            str(approval.get("principal") or "") != route_context.principal
            or str(approval.get("session_id") or "") != route_context.session_id
            or str(approval.get("source") or "") != route_context.source
        ):
            return _approval_failure(task, "resume_context_mismatch")
    elif route_context.source != "desktop":
        return _approval_failure(task, "resume_context_missing")
    execution_grant: ExecutionGrant | None = None
    provider_policy: dict | None = None
    capabilities = frozenset()
    if stored_capabilities:
        approval_id = str(approval.get("approval_id") or "")
        record = _tp.load_consumed_operative_approval(approval_id, run_id=run_id)
        if not record:
            return _approval_failure(task, "resume_approval_missing")
        try:
            manifest = ExecutionManifest.from_dict(record["manifest"])
        except (KeyError, ApprovalError):
            return _approval_failure(task, "resume_approval_invalid")
        if (
            manifest.digest != str(record.get("manifest_digest") or "")
            or manifest.digest != str(approval.get("manifest_digest") or "")
            or manifest.task != task
            or manifest.plan != raw_plan
            or manifest.capabilities != tuple(sorted(stored_capabilities))
            or not _manifest_matches_runtime(manifest)
            or not validate_manifest_semantics(manifest)
        ):
            return _approval_failure(task, "resume_manifest_mismatch")
        grant_expires_at = str(record.get("grant_expires_at") or "")
        try:
            grant_expiry = datetime.datetime.fromisoformat(
                grant_expires_at.replace("Z", "+00:00")
            )
            if grant_expiry.tzinfo is None:
                grant_expiry = grant_expiry.replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            return _approval_failure(task, "resume_grant_invalid")
        if grant_expiry <= datetime.datetime.now(datetime.timezone.utc):
            return _approval_failure(task, "resume_grant_expired")
        execution_grant = ExecutionGrant(
            approval_id=approval_id,
            manifest_digest=manifest.digest,
            principal=manifest.principal,
            session_id=manifest.session_id,
            source=manifest.source,
            run_id=run_id,
            grant_expires_at=grant_expires_at,
            capabilities=manifest.capabilities,
            resources_json=manifest.resources_json,
        )
        provider_policy = manifest.provider_policy
        capabilities = frozenset(execution_grant.capabilities)

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

    if execution_grant is not None and attempted_numbers - done_numbers:
        result = _approval_failure(task, "reapproval_required")
        result["steps"] = steps
        result["execution_approval"] = approval
        return result

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
        approval=approval,
        created_at=task_created_at,
    )
    if not persistence_ok:
        stop_reason = "task_persistence_failed"
        _prog("Task state could not be persisted", "execution blocked")

    grant_scope = execution_grant.to_scope() if execution_grant else None
    with execution_capability_scope(
        capabilities,
        deadline=deadline,
        sensitive_step_numbers=persisted_sensitive_steps,
        unavailable_step_numbers=persisted_sensitive_steps,
        require_resource_grant=execution_grant is not None,
        provider_policy=provider_policy,
    ), safety_permissions.execution_grant_scope(grant_scope):
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
                approval=approval,
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
                approval=approval,
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
                if execution_grant is not None:
                    if _step_may_have_side_effect(step):
                        stop_reason = "uncertain_side_effect_outcome"
                        _prog(
                            "Side-effect outcome requires reconciliation",
                            "automatic recovery is blocked",
                        )
                    else:
                        stop_reason = "reapproval_required"
                        _prog("Recovery requires a new approval", "approved plan cannot change")
                    break
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
                    approval=approval,
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
                            approval=approval,
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
            approval=approval,
            created_at=task_created_at,
            outcome=stop_reason or ("succeeded" if task_ok else "failed"),
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
        "execution_approval": approval,
    }


def run_task_async(
    task: str,
    on_progress: Callable[[str, str], None] | None = None,
    on_complete: Callable[[dict], None] | None = None,
    cancel_event: threading.Event | None = None,
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
