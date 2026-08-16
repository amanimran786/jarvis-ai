"""Legacy Jarvis orchestration telemetry and proposal loop.

Coordination v2 makes Codex and ``harness.agent_coordinator`` authoritative for
engineering assignment, leasing, verification, and completion. This module may
observe legacy sessions and generate non-executable proposals, but it must not
dispatch engineering tasks or transition coordination-v2 queue rows.

Entry point:
    python orchestrator_loop.py [--dry-run] [--max-concurrent N]
or as a library:
    from orchestrator_loop import run_loop
    run_loop(max_concurrent=3, dry_run=False)
"""
from __future__ import annotations

import argparse
import datetime
import http.client
import json
import logging
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Literal

# ── Bootstrap: ensure repo root is on sys.path ────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from harness.session_tracker import SessionTracker  # noqa: E402
from harness.commit_review_gate import CommitGateResult, write_gate_log  # noqa: E402
from harness.completion_verifier import (  # noqa: E402
    CompletionEvidenceError,
    compact_completion_evidence,
    verify_completion,
)
from harness.retry_policy import RetryAction, RetryDecision, decide_retry  # noqa: E402
from harness.capability_checker import check_contract_capabilities  # noqa: E402
from harness.approval_workflow import consume_approval, restore_approval  # noqa: E402
from harness.state_lock import queue_state_lock  # noqa: E402
from harness.task_contract import (  # noqa: E402
    AttemptRecord,
    AttemptStore,
    CompletionVerdict,
    ContractError,
    TaskContract,
    TaskSpec,
    approval_logged,
    contract_for_task,
    normalized_task_spec_digest,
    task_contract_digest,
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
PRE_COMMIT_VIOLATIONS_LOG = _REPO_ROOT / "logs" / "pre_commit_violations.log"
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

# ── Ops dashboard ──────────────────────────────────────────────────────────────

_DASHBOARD_THREAD: threading.Thread | None = None
_DASHBOARD_LOCK   = threading.Lock()


def _dashboard_port_state(port: int) -> Literal["dashboard", "occupied", "free"]:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            pass
    except OSError:
        return "free"

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
    try:
        connection.request("GET", "/api/status")
        response = connection.getresponse()
        body = response.read(512)
        if response.status == 401 and b"dashboard_auth_required" in body:
            return "dashboard"
    except (OSError, http.client.HTTPException):
        pass
    finally:
        connection.close()
    return "occupied"


def _ensure_dashboard_running(port: int = 7842) -> None:
    """Start jarvis_dashboard on *port* in a daemon thread (one per process).

    Safe to call every loop iteration — re-entrancy is guarded by
    ``_DASHBOARD_LOCK`` and the ``is_alive()`` check.  If the dashboard
    module or uvicorn is unavailable the failure is logged and swallowed so
    the orchestrator loop continues normally.
    """
    global _DASHBOARD_THREAD
    with _DASHBOARD_LOCK:
        if _DASHBOARD_THREAD is not None and _DASHBOARD_THREAD.is_alive():
            return  # already running
        port_state = _dashboard_port_state(port)
        if port_state == "dashboard":
            log.info("[dashboard] already serving on loopback port %d", port)
            return
        if port_state == "occupied":
            log.warning(
                "[dashboard] port %d is occupied by a non-dashboard process", port
            )
            return

        def _run() -> None:
            try:
                import uvicorn
                from jarvis_dashboard import app as _dash_app
                uvicorn.run(
                    _dash_app,
                    host="127.0.0.1",
                    port=port,
                    log_level="warning",
                    access_log=False,
                )
            except OSError as exc:
                # Port already in use — another process is running the dashboard.
                log.info("[dashboard] port %d already in use (%s) — skipping", port, exc)
            except Exception as exc:
                log.warning("[dashboard] failed to start: %s", exc)

        _DASHBOARD_THREAD = threading.Thread(
            target=_run, name="JarvisDashboard", daemon=True
        )
        _DASHBOARD_THREAD.start()
        if sys.stdout.isatty():
            try:
                import webbrowser
                from jarvis_dashboard import dashboard_bootstrap_url

                webbrowser.open(dashboard_bootstrap_url("127.0.0.1", port))
            except Exception as exc:
                log.debug("[dashboard] could not open authenticated browser: %s", exc)
        log.info(
            "[dashboard] started on loopback port %d; run jarvis_dashboard.py "
            "to authenticate an existing server",
            port,
        )


def run_loop(max_concurrent: int = 3, dry_run: bool = False) -> dict[str, Any]:
    global _LOG_BUFFER
    with queue_state_lock(WORK_QUEUE_PATH):
        try:
            return _run_loop(max_concurrent=max_concurrent, dry_run=dry_run)
        finally:
            if _LOG_BUFFER is not None:
                buffered, _LOG_BUFFER = _LOG_BUFFER, None
                _write_master_lines(buffered)


def _run_loop(max_concurrent: int = 3, dry_run: bool = False) -> dict[str, Any]:
    """
    Execute one iteration of the orchestration loop.

    Returns a summary dict:
    {
        "harvested":   int,   # tasks marked done
        "needs_review": int,  # tasks that passed verification but failed the pre-commit gate
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
        "needs_review": 0,
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

    # Buffer this iteration's MASTER_LOG lines; an idle loop is discarded below.
    global _LOG_BUFFER
    _LOG_BUFFER = []

    # Start the ops dashboard (idempotent — skipped if already running).
    _ensure_dashboard_running()

    _log_master(f"[orchestrator] loop start — max_concurrent={max_concurrent} dry_run={dry_run}")

    # ── Step 0: Auto-expire stalled sessions ──────────────────────────────────
    # LEGACY handoff-ready sessions can get stuck as status=active when no
    # Cowork session opens them, holding all concurrent slots indefinitely.
    # Expire any session silent for more than 90 minutes and requeue its task.
    if not dry_run:
        _expire_stalled_sessions(tracker, timeout_minutes=90)

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
        if int(task.get("coordination_version") or 0) >= 2:
            tracker.purge_completed([sid])
            summary["rejected"] += 1
            _log_master(
                f"[orchestrator] coordination-v2 completion ignored — "
                f"task={task_id}; submit through harness.agent_coordinator"
            )
            continue
        if (
            task.get("status") != "in_progress"
            or task.get("assigned_to") != sid
        ):
            tracker.purge_completed([sid])
            summary["rejected"] += 1
            _log_master(
                f"[orchestrator] stale completion quarantined — "
                f"task={task_id} session={sid}"
            )
            log.warning(
                "[Loop] stale completion quarantined for task %s (session %s)",
                task_id,
                sid,
            )
            continue
        try:
            spec = TaskSpec.from_queue_task(task)
        except ContractError as exc:
            _mark_task_verification(
                task_id,
                "blocked",
                "invalid_contract",
                [str(exc)],
                {},
                expected_assignee=sid,
            )
            summary["rejected"] += 1
            continue

        evidence: dict[str, Any] = {}
        gate_result: CommitGateResult | None = None
        runtime_failure = str(session.get("runtime_failure_class") or "")
        if runtime_failure:
            verdict = CompletionVerdict(
                "rejected",
                runtime_failure,
                (summary_text or "runtime execution failed",),
            )
        elif (
            session.get("repo_path")
            and session.get("base_ref")
            and session.get("completion_commit")
        ):
            try:
                assessment = verify_completion(
                    spec,
                    session["repo_path"],
                    session["base_ref"],
                    completion_ref=session["completion_commit"],
                )
            except CompletionEvidenceError as exc:
                _log_master(
                    f"[orchestrator] evidence collection failed for {task_id}: {exc}"
                )
                verdict = CompletionVerdict(
                    "unverified",
                    "infrastructure_failure",
                    (str(exc),),
                )
            else:
                evidence = dict(assessment.evidence)
                gate_result = assessment.gate
                verdict = assessment.verdict
                if gate_result.infrastructure_failed:
                    verdict = CompletionVerdict(
                        "unverified",
                        "infrastructure_failure",
                        tuple(gate_result.reasons()),
                    )
        else:
            verdict = CompletionVerdict(
                "unverified",
                "verification_missing",
                (
                    "completion provenance is missing repo_path, base_ref, "
                    "or completion_commit",
                ),
            )

        stored_hash = str(session.get("contract_sha256") or "")
        if runtime_failure:
            pass
        elif stored_hash and stored_hash != spec.contract_hash:
            gate_result = None
            verdict = CompletionVerdict(
                "rejected",
                "contract_mismatch",
                ("task contract changed after dispatch",),
            )

        attempt_id = str(session.get("attempt_id") or f"untracked_{sid}")
        attempt_number = int(session.get("attempt_number") or 1)
        persisted_evidence = compact_completion_evidence(evidence)
        completion_checkpoint = AttemptRecord.checkpoint(
            spec,
            sid,
            attempt_id,
            phase="completion_gate",
            status=verdict.status,
            evidence=persisted_evidence,
            failure_class=verdict.failure_class,
            attempt_number=attempt_number,
        )
        try:
            attempt_store.append(completion_checkpoint)
        except OSError as exc:
            gate_result = None
            verdict = CompletionVerdict(
                "unverified",
                "checkpoint_failure",
                (f"completion checkpoint write failed: {exc}",),
            )

        if (
            gate_result is not None
            and not gate_result.passed
            and not gate_result.infrastructure_failed
        ):
            try:
                write_gate_log(
                    PRE_COMMIT_VIOLATIONS_LOG,
                    task_id,
                    sid,
                    gate_result,
                    timestamp=_now(),
                )
            except OSError:
                log.exception("[Loop] could not write redacted pre-commit violations")
            _mark_task_verification(
                task_id,
                "needs_review",
                "pre_commit_gate_violation",
                gate_result.reasons(),
                persisted_evidence,
                expected_assignee=sid,
            )
            summary["needs_review"] += 1
            _log_master(
                f"[orchestrator] {sid} → task {task_id} NEEDS_REVIEW — "
                f"commit gate found {len(gate_result.findings)} finding(s)"
            )
            log.warning(
                "[Loop] Task %s needs_review — pre-commit gate violations (session %s)",
                task_id, sid,
            )
        elif verdict.passed and _mark_task_done(
            task_id,
            summary_text,
            persisted_evidence,
            expected_assignee=sid,
        ):
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
                _mark_task_retry(
                    task_id,
                    decision,
                    expected_assignee=sid,
                )
                queue_status = "queued"
                summary["retried"] += 1
            elif decision.action is RetryAction.ROUTE_TO_VERIFIER:
                queue_status = "unverified"
                _mark_task_verification(
                    task_id,
                    queue_status,
                    verdict.failure_class,
                    list(verdict.reasons),
                    persisted_evidence,
                    next_action=decision.action.value,
                    expected_assignee=sid,
                )
                summary["unverified"] += 1
            else:
                queue_status = "blocked"
                _mark_task_verification(
                    task_id,
                    queue_status,
                    verdict.failure_class,
                    list(verdict.reasons),
                    persisted_evidence,
                    next_action=decision.action.value,
                    expected_assignee=sid,
                )
                summary["rejected"] += 1
            _log_master(
                f"[orchestrator] {sid} → task {task_id} {queue_status.upper()} "
                f"({verdict.failure_class}; {decision.action.value})"
            )
    tracker.purge_completed(
        str(session.get("session_id") or "")
        for session in completed
    )

    # ── Step 2: Suggest follow-up tasks for completed work ────────────────────
    for session in newly_done:
        task_id = session.get("task_id", "")
        task    = _find_task(task_id)
        if task is None or task.get("status") != "done":
            continue
        follow_ups = _suggest_follow_ups(task, dry_run=dry_run)
        for ft in follow_ups:
            _enqueue_task(ft)
            summary["follow_ups"] += 1
            _log_master(
                f"[orchestrator] follow-up proposed for Codex review: "
                f"{ft.get('id')} — {ft.get('title')}"
            )

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
        # Every autonomous queue row must resolve a valid typed TaskContract.
        # Explicit TaskSpec IDs are execution metadata, not authorization.
        contract = _resolve_typed_contract(task, spec)
        if contract is None:
            _mark_task_blocked(
                task,
                "autonomous execution requires an explicit typed task contract",
            )
            summary["blocked"] += 1
            _log_master(
                f"[orchestrator] {spec.task_id} blocked — typed contract is missing"
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

        spec = spec.for_dispatch()
        contract_digest = task_contract_digest(contract)
        spec_digest = normalized_task_spec_digest(spec)
        if contract.task_spec_sha256 != spec_digest:
            _mark_task_blocked(
                task,
                "typed task contract does not match the executable task specification",
            )
            summary["blocked"] += 1
            _log_master(
                f"[orchestrator] {spec.task_id} blocked — typed contract "
                f"{contract.task_id} is bound to a different task specification"
            )
            continue
        if contract.requires_approval and not approval_logged(
            contract.task_id,
            task_contract_sha256=contract_digest,
            task_spec_sha256=spec_digest,
        ):
            _mark_task_awaiting_approval(task)
            summary["blocked"] += 1
            _log_master(
                f"[orchestrator] {spec.task_id} awaiting digest-bound approval — "
                f"contract {contract.task_id}"
            )
            continue

        capability_results = check_contract_capabilities(contract)
        missing_capabilities = [
            name for name, available in capability_results.items() if not available
        ]
        if missing_capabilities:
            _log_master(
                f"[orchestrator] {spec.task_id} missing capabilities (log-only): "
                f"{', '.join(missing_capabilities)}"
            )
        _mark_task_contract_validated(task, contract)
        _log_master(
            f"[orchestrator] {spec.task_id} typed contract {contract.task_id} "
            f"v{contract.contract_version} validated — dispatching"
        )

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
            consumed_approval = None
            if contract.requires_approval:
                consumed_approval = consume_approval(
                    contract.task_id,
                    task_contract_sha256=contract_digest,
                    task_spec_sha256=spec_digest,
                )
                if consumed_approval is None:
                    _mark_task_awaiting_approval(task)
                    summary["blocked"] += 1
                    _log_master(
                        f"[orchestrator] {spec.task_id} approval was already consumed; "
                        "launch cancelled"
                    )
                    continue
            try:
                attempt_store.append(attempt)
            except OSError as exc:
                _restore_launch_approval(consumed_approval, spec.task_id)
                _mark_task_blocked(task, f"checkpoint write failed: {exc}")
                summary["blocked"] += 1
                _log_master(
                    f"[orchestrator] {spec.task_id} blocked — checkpoint write failed: {exc}"
                )
                continue
            launch_result = _write_launch_record(launch_record)
            if launch_result == "duplicate":
                _mark_task_blocked(task, "launch queue already has a pending record")
                summary["blocked"] += 1
                _log_master(
                    f"[orchestrator] {spec.task_id} blocked — launch already pending; "
                    "approval remains consumed"
                )
                continue
            if launch_result == "failed":
                _restore_launch_approval(consumed_approval, spec.task_id)
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

    # Flush the buffer only when the iteration did meaningful work; a purely
    # idle poll (all counters zero) is dropped so the log stops growing on every
    # scheduled run.  active_now is excluded — a steady-state active session is
    # not new activity worth re-logging.
    buffered, _LOG_BUFFER = _LOG_BUFFER or [], None
    did_work = any(
        summary[k]
        for k in ("harvested", "needs_review", "follow_ups", "launched", "blocked",
                  "unverified", "rejected", "retried")
    )
    if did_work:
        _write_master_lines(buffered)
    return summary


# ── Stale-session expiry ──────────────────────────────────────────────────────

def _expire_stalled_sessions(tracker: SessionTracker, timeout_minutes: int = 90) -> int:
    """
    Auto-expire sessions that have been 'active' with no updates for too long.

    LEGACY handoff-ready sessions get stuck as status=active when no Cowork
    session opens them, permanently consuming concurrent slots.  This runs at
    the top of every loop iteration so the blockage is self-healing.

    Returns the number of sessions expired.
    """
    stalled = tracker.expire_stalled(timeout_minutes=timeout_minutes)
    if not stalled:
        return 0

    # Requeue any WORK_QUEUE tasks that were in_progress for these sessions
    queue = _load_queue()
    stalled_session_ids = {s["session_id"] for s in stalled}
    requeued: list[str] = []
    for task in queue:
        if int(task.get("coordination_version") or 0) >= 2:
            continue
        if (
            task.get("status") == "in_progress"
            and task.get("assigned_to") in stalled_session_ids
        ):
            task["status"] = "unverified"
            task["assigned_to"] = None
            task["assigned_at"] = None
            task["verification_failure_class"] = "legacy_stalled_session"
            task["verification_reasons"] = [
                "legacy session stalled; Codex must review before reassignment"
            ]
            notes = str(task.get("notes") or "")
            task["notes"] = (
                notes
                + f" | quarantined {_now()[:10]}: session stalled (>{timeout_minutes}min)"
            )
            requeued.append(str(task.get("id") or task.get("session_name") or "?"))

    if requeued:
        _save_queue(queue)

    _log_master(
        f"[orchestrator] auto-expired {len(stalled)} stalled session(s)"
        + (f" — quarantined: {', '.join(requeued)}" if requeued else " (no in_progress tasks)")
    )
    log.info(
        "[Loop] Auto-expired %d stalled session(s); quarantined tasks: %s",
        len(stalled),
        requeued or "none",
    )
    return len(stalled)


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
        log.exception("[Loop] Could not save WORK_QUEUE.json")
        raise OSError(f"could not save {WORK_QUEUE_PATH}") from exc


def _find_task(task_id: str) -> dict | None:
    return next((t for t in _load_queue() if _task_identity(t) == task_id), None)


def _task_identity(task: dict[str, Any]) -> str:
    try:
        return TaskSpec.from_queue_task(task).task_id
    except ContractError:
        return str(task.get("id") or "")


def _require_legacy_task(task: dict[str, Any], operation: str) -> None:
    if int(task.get("coordination_version") or 0) >= 2:
        raise RuntimeError(
            f"coordination-v2 task requires harness.agent_coordinator: {operation}"
        )


def _pick_next_queued() -> dict | None:
    """Return no work; Codex assignments are claimed through the coordinator."""
    return None


def _mark_task_done(
    task_id: str,
    result_summary: str,
    evidence: dict[str, Any] | None = None,
    *,
    expected_assignee: str = "",
) -> bool:
    """Store verified worker output for Codex review without completing it."""
    if not task_id:
        return False
    queue = _load_queue()
    for task in queue:
        if _task_identity(task) == task_id:
            _require_legacy_task(task, "completion")
            if task.get("orchestrated_by") != "codex":
                raise RuntimeError(
                    f"task was not assigned by Codex: {task_id}"
                )
            if expected_assignee and (
                task.get("status") != "in_progress"
                or task.get("assigned_to") != expected_assignee
            ):
                raise RuntimeError(
                    f"task assignment changed before completion: {task_id}"
                )
            task["status"] = "awaiting_codex_review"
            task["candidate_result_summary"] = result_summary
            task["candidate_completed_at"] = _now()
            task["orchestration_state"] = "awaiting_codex_review"
            if evidence:
                task["completion_evidence"] = evidence
                task["completion_commit"] = evidence.get("completion_commit")
            _save_queue(queue)
            return True
    return False


def _mark_task_in_progress(task_id: str, session_id: str) -> None:
    queue = _load_queue()
    for task in queue:
        if _task_identity(task) == task_id:
            _require_legacy_task(task, "lease")
            if (
                task.get("orchestrated_by") != "codex"
                or task.get("orchestration_state") != "assigned"
            ):
                raise RuntimeError(f"task is not assigned by Codex: {task_id}")
            task["status"]      = "in_progress"
            task["assigned_to"] = session_id
            task["assigned_at"] = _now()
            task["orchestration_state"] = "leased"
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
    expected_assignee: str = "",
) -> None:
    queue = _load_queue()
    for task in queue:
        if _task_identity(task) == task_id:
            _require_legacy_task(task, "verification")
            if expected_assignee and (
                task.get("status") != "in_progress"
                or task.get("assigned_to") != expected_assignee
            ):
                raise RuntimeError(
                    f"task assignment changed before verification: {task_id}"
                )
            task["status"] = status
            task["verification_failure_class"] = failure_class
            task["verification_reasons"] = reasons
            task["completion_evidence"] = evidence
            task["next_action"] = next_action
            task["verified_at"] = _now()
            break
    else:
        raise RuntimeError(f"task disappeared before verification update: {task_id}")
    _save_queue(queue)


def _mark_task_retry(
    task_id: str,
    decision: RetryDecision,
    *,
    expected_assignee: str = "",
) -> None:
    queue = _load_queue()
    for task in queue:
        if _task_identity(task) == task_id:
            _require_legacy_task(task, "retry")
            if expected_assignee and (
                task.get("status") != "in_progress"
                or task.get("assigned_to") != expected_assignee
            ):
                raise RuntimeError(
                    f"task assignment changed before retry: {task_id}"
                )
            task["status"] = "queued"
            task["attempt_number"] = decision.attempt_number + 1
            task["retry_failure_class"] = decision.failure_class.value
            task["retry_reason"] = decision.reason
            task["assigned_to"] = None
            task["assigned_at"] = None
            break
    else:
        raise RuntimeError(f"task disappeared before retry update: {task_id}")
    _save_queue(queue)


def _mark_task_blocked(task_to_block: dict[str, Any], reason: str) -> None:
    queue = _load_queue()
    identity = _task_identity(task_to_block)
    for task in queue:
        if task == task_to_block or (identity and _task_identity(task) == identity):
            _require_legacy_task(task, "block")
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
            _require_legacy_task(task, "approval")
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
            _require_legacy_task(task, "contract validation")
            task["contract_validated_at"] = _now()
            task["contract_version"] = contract.contract_version
            break
    _save_queue(queue)


def _enqueue_task(task: dict[str, Any]) -> None:
    queue = _load_queue()
    # Deduplicate by id
    if any(t.get("id") == task.get("id") for t in queue):
        return
    task["status"] = "proposed"
    task["requires_codex_assignment"] = True
    task["assigned_ai"] = None
    task["assigned_to"] = None
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


def _write_launch_record(
    record: dict[str, Any],
) -> Literal["written", "duplicate", "failed"]:
    """Append a launch record and distinguish deduplication from I/O failure."""
    queue = _load_launch_queue()
    # Deduplicate by task_id+status=pending
    existing_ids = {r["task_id"] for r in queue if r.get("status") == "pending"}
    if record["task_id"] in existing_ids:
        log.debug("[Loop] task %s already pending in LAUNCH_QUEUE — skipping", record["task_id"])
        return "duplicate"
    queue.append(record)
    tmp = str(LAUNCH_QUEUE_PATH) + ".loop.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(queue, f, indent=2)
        os.replace(tmp, str(LAUNCH_QUEUE_PATH))
        return "written"
    except Exception as exc:
        log.error("[Loop] Could not write LAUNCH_QUEUE.json: %s", exc)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return "failed"


# ── Follow-up task generation ─────────────────────────────────────────────────

_FOLLOWUP_SYSTEM = """\
You generate non-executable engineering proposals for Codex review. Given a \
completed Jarvis task, suggest 0-3 logical follow-up proposals. Respond with a \
JSON array only, no explanation. Each element: \
{"id": "TASK-NNN", "title": "...", "description": "...", \
"files_hint": [...], "acceptance_criteria": [...], "domain": "...", "priority": 2}. \
Do not choose a worker or claim that a proposal is approved. \
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
            ft["status"] = "proposed"
            ft["proposed_by"] = "local"
            ft["requires_codex_assignment"] = True
            ft["assigned_ai"] = None
            ft["assigned_to"] = None
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
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
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
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
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


# When a run_loop iteration is in progress we buffer its log lines instead of
# writing them immediately.  An idle iteration (no work harvested/launched/etc.)
# is discarded at flush time so the launchd job stops appending ~3 lines to
# MASTER_LOG.md every few minutes — the churn that kept the git tree dirty and
# bloated the log to hundreds of KB.  Buffering is off outside run_loop, so any
# other caller writes through immediately as before.
_LOG_BUFFER: list[str] | None = None


def _log_master(message: str) -> None:
    """Append a timestamped line to MASTER_LOG.md (buffered inside run_loop)."""
    line = f"[{datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC] {message}\n"
    if _LOG_BUFFER is not None:
        _LOG_BUFFER.append(line)
        return
    _write_master_lines([line])


def _write_master_lines(lines: list[str]) -> None:
    if not lines:
        return
    try:
        with open(MASTER_LOG_PATH, "a", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception as exc:
        log.debug("[Loop] Could not write MASTER_LOG.md: %s", exc)


def _restore_launch_approval(
    approval_record: dict[str, Any] | None, task_id: str
) -> None:
    """Return a consumed approval when dispatch did not become durable."""
    if approval_record is None:
        return
    try:
        restored = restore_approval(approval_record)
    except Exception as exc:
        _log_master(
            f"[orchestrator] CRITICAL: could not restore approval for {task_id}: {exc}"
        )
        return
    if not restored:
        _log_master(
            f"[orchestrator] approval for {task_id} was already restored"
        )


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
