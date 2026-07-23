"""Bridge durable launch handoffs to local task_runtime executions."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from harness.commit_review_gate import CommitGateError, capture_clean_head
from harness.runtime_adapter import (
    RuntimeMissingOutcome,
    RuntimePendingOutcome,
    RuntimeTerminalOutcome,
    RuntimeTerminalStatus,
    TaskRuntimeAdapter,
)
from harness.session_tracker import SessionTracker
from harness.task_contract import ContractError, TaskSpec


RUNTIME_PENDING_STATUSES = frozenset(
    {"queued", "assigned", "running", "streaming", "waiting_approval", "timeout_pending"}
)


def process_runtime_queue(
    queue_path: str | Path,
    *,
    task_runtime_module: Any | None = None,
    tracker: SessionTracker | None = None,
) -> list[dict[str, Any]]:
    """Submit ready handoffs and poll existing local runtime tasks once."""
    path = Path(queue_path)
    queue = _load_queue(path)
    if not queue:
        return []
    actionable = any(
        str(entry.get("status") or "") in ({"handoff_ready", "fired"} | RUNTIME_PENDING_STATUSES)
        or bool(entry.get("runtime_correlation"))
        for entry in queue
        if isinstance(entry, dict)
    )
    if not actionable:
        return []
    if task_runtime_module is None:
        import task_runtime as task_runtime_module  # type: ignore[no-redef]

    session_tracker = tracker or SessionTracker()
    adapter = TaskRuntimeAdapter(task_runtime_module)
    changed: list[dict[str, Any]] = []

    for entry in queue:
        status = str(entry.get("status") or "")
        if status == "fired" and not entry.get("runtime_correlation"):
            session_tracker.remove(str(entry.get("session_id") or ""))
            entry["status"] = "legacy_stale"
            entry["runtime_error"] = "pickup was marked fired without a runtime task"
            changed.append(entry)
            continue

        if status == "handoff_ready":
            try:
                spec = TaskSpec.from_queue_task(entry.get("task_spec") or {})
                if spec.legacy_adapter:
                    raise ContractError("legacy task contract cannot execute autonomously")
                if spec.contract_hash != str(entry.get("contract_sha256") or ""):
                    raise ContractError("launch contract hash mismatch")
                correlation = adapter.submit(
                    spec,
                    str(entry.get("prompt") or ""),
                    attempt_id=str(entry.get("attempt_id") or ""),
                    base_ref=str(entry.get("base_ref") or ""),
                )
            except Exception as exc:
                session_tracker.remove(str(entry.get("session_id") or ""))
                entry["status"] = "runtime_error"
                entry["runtime_error"] = f"{type(exc).__name__}: {exc}"
                changed.append(entry)
                continue

            session_tracker.remove(str(entry.get("session_id") or ""))
            session_tracker.claim(
                spec.task_id,
                correlation.runtime_task_id,
                attempt_id=correlation.attempt_id,
                contract_sha256=correlation.contract_hash,
                attempt_number=int(entry.get("attempt_number") or 1),
                repo_path=correlation.worktree_path,
                base_ref=correlation.base_ref,
            )
            entry["runtime_correlation"] = correlation.to_dict()
            entry["runtime_task_id"] = correlation.runtime_task_id
            entry["status"] = "queued"
            changed.append(entry)

        if entry.get("runtime_correlation") and str(entry.get("status")) in RUNTIME_PENDING_STATUSES:
            from harness.runtime_adapter import RuntimeCorrelation

            correlation = RuntimeCorrelation.from_dict(entry["runtime_correlation"])
            outcome = adapter.poll(correlation)
            if isinstance(outcome, RuntimePendingOutcome):
                entry["status"] = outcome.status.value
                if outcome.approval_reason:
                    entry["approval_reason"] = outcome.approval_reason
            elif isinstance(outcome, RuntimeTerminalOutcome):
                if outcome.status is RuntimeTerminalStatus.SUCCEEDED:
                    try:
                        completion_commit = capture_clean_head(
                            correlation.worktree_path
                        )
                    except (CommitGateError, OSError, RuntimeError, ValueError) as exc:
                        session_tracker.fail(
                            correlation.runtime_task_id,
                            f"completion commit unavailable: {type(exc).__name__}",
                            failure_class="infrastructure_failure",
                        )
                        entry["status"] = "runtime_error"
                        entry["runtime_error"] = "completion commit unavailable"
                    else:
                        completion_persisted = session_tracker.complete(
                            correlation.runtime_task_id,
                            outcome.result,
                            completion_commit=completion_commit,
                        )
                        if not completion_persisted:
                            entry["status"] = "runtime_error"
                            entry["runtime_error"] = (
                                "completion session was not found"
                            )
                        else:
                            entry["status"] = "completion_claimed"
                else:
                    message = outcome.error or outcome.result or outcome.status.value
                    session_tracker.fail(correlation.runtime_task_id, message)
                    entry["status"] = outcome.status.value
                    entry["runtime_error"] = message
            elif isinstance(outcome, RuntimeMissingOutcome):
                session_tracker.fail(
                    correlation.runtime_task_id,
                    outcome.error,
                    failure_class="infrastructure_failure",
                )
                entry["status"] = "runtime_missing"
                entry["runtime_error"] = outcome.error
            changed.append(entry)

    if changed:
        _save_queue(path, queue)
    return changed


def _load_queue(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def _save_queue(path: Path, queue: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".runtime.tmp")
    try:
        tmp.write_text(json.dumps(queue, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
