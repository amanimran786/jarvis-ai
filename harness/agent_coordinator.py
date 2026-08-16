"""Atomic Claude/Codex task leasing for the shared Jarvis checkout.

The coordinator deliberately permits one active engineering lease in the
shared checkout. Parallel agents require isolated worktrees and a separate
merge arbiter; counting two agents in one directory is not safe concurrency.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import logging
import os
import subprocess
import sys
import uuid
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from harness.approval_workflow import consume_approval, restore_approval
from harness.completion_verifier import (
    CompletionEvidenceError,
    compact_completion_evidence,
    verify_completion,
)
from harness.state_lock import queue_state_lock
from harness.task_contract import (
    APPROVED_TASKS_PATH,
    TASK_CONTRACTS_PATH,
    ContractError,
    SideEffect,
    TaskContract,
    TaskSpec,
    approval_logged,
    load_contracts,
    normalized_task_spec_digest,
    task_contract_digest,
    validate_contract,
)
from runtime_state import app_data_dir


REPO_ROOT = Path(__file__).resolve().parent.parent
WORK_QUEUE_PATH = REPO_ROOT / "WORK_QUEUE.json"
SUPPORTED_AGENTS = frozenset({"claude", "codex"})
ACTIVE_STATUSES = frozenset({"active", "in_progress", "running"})
ASSIGNABLE_STATUSES = frozenset({
    "awaiting_approval",
    "blocked",
    "needs_review",
    "proposed",
    "queued",
    "unverified",
})
ORCHESTRATION_STAGES = frozenset({"poc", "implementation", "hardening", "release"})
CONTROL_PLANE_AGENT = "codex"
CODEX_REVIEW_STATUS = "awaiting_codex_review"
COORDINATION_VERSION = 2
DEFAULT_LEASE_SECONDS = 3600
LEASE_EXPIRY_COOLDOWN_SECONDS = 1200
log = logging.getLogger(__name__)

_LEASE_FIELDS = (
    "lease_owner",
    "lease_id",
    "lease_acquired_at",
    "lease_expires_at",
    "lease_base_ref",
    "lease_previous_assignee",
    "lease_contract_sha256",
    "lease_task_spec_sha256",
)


class CoordinationError(RuntimeError):
    """Raised when shared queue state cannot be changed safely."""


def default_state_path() -> Path:
    return app_data_dir() / "orchestrator" / "agent_coordination.json"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat()


def _parse_time(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _normalize_agent(agent: str) -> str:
    normalized = str(agent or "").strip().lower()
    if normalized not in SUPPORTED_AGENTS:
        raise CoordinationError(
            f"agent must be one of: {', '.join(sorted(SUPPORTED_AGENTS))}"
        )
    return normalized


def _assigned_agent(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in SUPPORTED_AGENTS:
        return text
    return "unknown"


def _task_id(task: Mapping[str, Any]) -> str:
    return str(
        task.get("contract_id") or task.get("id") or task.get("session_name") or ""
    ).strip()


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoordinationError(f"could not read {path}: {exc}") from exc
    if not isinstance(payload, list) or not all(
        isinstance(item, dict) for item in payload
    ):
        raise CoordinationError(f"{path} must contain a JSON list of objects")
    return payload


def _load_state(path: Path) -> dict[str, Any]:
    if not Path(path).exists():
        return {"version": COORDINATION_VERSION, "agents": {}}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoordinationError(f"could not read {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("agents", {}), dict):
        raise CoordinationError(f"{path} must contain an agent coordination object")
    payload.setdefault("version", COORDINATION_VERSION)
    payload.setdefault("agents", {})
    return payload


def _atomic_write_json(path: Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = (path.stat().st_mode & 0o777) if path.exists() else 0o600
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            mode,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _run_git(repo_path: Path, *args: str) -> str:
    """Run a read-only git query against the shared checkout.

    ``--no-optional-locks`` stops git from opportunistically refreshing and
    rewriting the on-disk index (the operation that briefly takes
    .git/index.lock). Without it, a timeout here kills git with SIGKILL
    mid-write and leaves an orphaned, zero-byte index.lock behind that blocks
    every future git command in the shared checkout until a human deletes it.
    _run_git is only ever used for read-only queries (status --porcelain,
    rev-parse) — do not add a write subcommand here without revisiting this
    flag.
    """
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", *args],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CoordinationError(f"could not run git {' '.join(args)}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise CoordinationError(detail or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _clean_repo_head(repo_path: Path) -> str:
    root = Path(repo_path).expanduser().resolve()
    status = _run_git(root, "status", "--porcelain", "--untracked-files=all")
    if status:
        raise CoordinationError(
            "shared checkout is dirty; commit or isolate existing work before claiming"
        )
    return _run_git(root, "rev-parse", "HEAD")


def _agent_record(state: dict[str, Any], agent: str) -> dict[str, Any]:
    agents = state.setdefault("agents", {})
    record = agents.setdefault(agent, {})
    if not isinstance(record, dict):
        record = {}
        agents[agent] = record
    return record


def _cooldown_until(
    state: Mapping[str, Any],
    agent: str,
    now: dt.datetime,
) -> dt.datetime | None:
    agents = state.get("agents", {})
    if not isinstance(agents, Mapping):
        return None
    record = agents.get(agent, {})
    if not isinstance(record, Mapping):
        return None
    until = _parse_time(record.get("cooldown_until"))
    return until if until is not None and until > now else None


def _clear_lease(task: dict[str, Any], *, restore_assignee: bool) -> None:
    previous = task.get("lease_previous_assignee")
    for field_name in _LEASE_FIELDS:
        task.pop(field_name, None)
    if restore_assignee:
        task["assigned_to"] = previous
        task["assigned_at"] = None


def _expire_leases(
    queue: list[dict[str, Any]],
    state: dict[str, Any],
    now: dt.datetime,
) -> list[str]:
    expired: list[str] = []
    for task in queue:
        if task.get("status") not in ACTIVE_STATUSES:
            continue
        owner = _assigned_agent(task.get("lease_owner"))
        expires_at = _parse_time(task.get("lease_expires_at"))
        if not _valid_v2_lease_shape(task, owner) or expires_at is None:
            task_id = _task_id(task)
            _clear_lease(task, restore_assignee=False)
            task.update(
                {
                    "status": "unverified",
                    "assigned_to": None,
                    "assigned_at": None,
                    "orchestration_state": "legacy_quarantined",
                    "verification_failure_class": "invalid_active_lease",
                    "verification_reasons": [
                        "active task had no valid coordination v2 lease"
                    ],
                    "verified_at": _iso(now),
                }
            )
            expired.append(task_id)
            continue
        if expires_at > now:
            continue
        task_id = _task_id(task)
        _clear_lease(task, restore_assignee=True)
        task["status"] = "queued"
        task["orchestration_state"] = "assigned"
        task["lease_expired_at"] = _iso(now)
        task["handoff_reason"] = f"{owner} lease expired"
        record = _agent_record(state, owner)
        cooldown_until = now + dt.timedelta(seconds=LEASE_EXPIRY_COOLDOWN_SECONDS)
        record.update(
            {
                "status": "cooldown",
                "cooldown_until": _iso(cooldown_until),
                "cooldown_reason": f"lease expired for {task_id}",
                "updated_at": _iso(now),
            }
        )
        expired.append(task_id)
    return expired


def _contract_for_queue_task(
    task: Mapping[str, Any],
    contracts: Mapping[str, TaskContract],
) -> tuple[TaskContract, TaskSpec, str, str]:
    task_id = _task_id(task)
    contract = contracts.get(task_id)
    if contract is None:
        raise CoordinationError(f"task {task_id or '<unknown>'} has no typed contract")
    is_valid, errors = validate_contract(contract)
    if not is_valid:
        raise CoordinationError(
            f"task {task_id} has an invalid contract: {'; '.join(errors)}"
        )
    try:
        spec = TaskSpec.from_queue_task(task).for_dispatch()
        spec_digest = normalized_task_spec_digest(spec)
    except ContractError as exc:
        raise CoordinationError(f"task {task_id} is not executable: {exc}") from exc
    if contract.task_spec_sha256 != spec_digest:
        raise CoordinationError(
            f"task {task_id} contract does not match its executable specification"
        )
    allowed_files = list(spec.allowed_files)
    for output in contract.outputs:
        raw_path = str(output.path_template or "").strip()
        if not raw_path:
            continue
        raw_path = raw_path.replace("{task_id}", spec.task_id)
        path = Path(raw_path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\x00" in raw_path
            or "{" in raw_path
            or "}" in raw_path
        ):
            raise CoordinationError(
                f"task {task_id} contract has an unsafe output path: {raw_path}"
            )
        allowed_files.append(raw_path)
    if (
        SideEffect.WRITES_FILES in contract.side_effects
        and not allowed_files
    ):
        raise CoordinationError(
            f"task {task_id} writes files but has no enforceable output scope"
        )
    verification_commands = list(spec.verification_commands)
    entry_point = str(contract.entry_point or "").strip()
    if entry_point:
        verification_commands.append(entry_point)
    effective_spec = replace(
        spec,
        allowed_files=tuple(dict.fromkeys(allowed_files)),
        verification_commands=tuple(dict.fromkeys(verification_commands)),
    )
    return contract, effective_spec, task_contract_digest(contract), spec_digest


def _codex_assignment_allows_claim(task: Mapping[str, Any], agent: str) -> bool:
    """Return whether Codex assigned this exact task to ``agent``."""
    return bool(
        str(task.get("orchestrated_by") or "").strip().lower()
        == CONTROL_PLANE_AGENT
        and task.get("orchestration_state") == "assigned"
        and _assigned_agent(task.get("worker_type")) == agent
        and _assigned_agent(task.get("assigned_to")) == agent
    )


def _poc_approval_digest(
    task_id: str,
    contract_digest: str,
    spec_digest: str,
    completion_commit: str,
) -> str:
    payload = "\n".join(
        (task_id, contract_digest, spec_digest, completion_commit)
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _approved_poc_digest(
    task: Mapping[str, Any],
    contracts: Mapping[str, TaskContract],
) -> str:
    task_id = _task_id(task)
    if (
        task.get("status") != "done"
        or task.get("orchestration_stage") != "poc"
        or task.get("poc_approved_by") != CONTROL_PLANE_AGENT
    ):
        raise CoordinationError(f"parent task {task_id} is not an approved Codex POC")
    _, _, contract_digest, spec_digest = _contract_for_queue_task(task, contracts)
    completion_commit = str(task.get("completion_commit") or "").strip()
    if not completion_commit:
        raise CoordinationError(f"parent POC {task_id} has no completion commit")
    expected = _poc_approval_digest(
        task_id,
        contract_digest,
        spec_digest,
        completion_commit,
    )
    if task.get("poc_approval_sha256") != expected:
        raise CoordinationError(f"parent POC {task_id} approval digest is invalid")
    return expected


def _validate_parent_poc(
    task: Mapping[str, Any],
    queue: list[dict[str, Any]],
    contracts: Mapping[str, TaskContract],
    *,
    require_assignment_binding: bool,
) -> str:
    parent_task_id = str(task.get("orchestration_parent_task_id") or "").strip()
    if not parent_task_id:
        return ""
    parent = next(
        (item for item in queue if _task_id(item) == parent_task_id),
        None,
    )
    if parent is None:
        raise CoordinationError(f"parent POC not found: {parent_task_id}")
    digest = _approved_poc_digest(parent, contracts)
    if (
        require_assignment_binding
        and task.get("orchestration_parent_poc_sha256") != digest
    ):
        raise CoordinationError("parent POC approval changed after assignment")
    return digest


def _valid_v2_lease_shape(
    task: Mapping[str, Any],
    owner: str | None,
) -> bool:
    return bool(
        owner in SUPPORTED_AGENTS
        and task.get("coordination_version") == COORDINATION_VERSION
        and task.get("orchestrated_by") == CONTROL_PLANE_AGENT
        and task.get("orchestration_id")
        and task.get("orchestration_state") == "leased"
        and _assigned_agent(task.get("worker_type")) == owner
        and _assigned_agent(task.get("assigned_to")) == owner
        and task.get("lease_id")
        and task.get("lease_base_ref")
        and task.get("lease_contract_sha256")
        and task.get("lease_task_spec_sha256")
    )


def assign_task(
    task_id: str,
    worker: str,
    *,
    stage: str,
    rationale: str,
    parent_task_id: str = "",
    queue_path: Path = WORK_QUEUE_PATH,
    contracts_path: Path = TASK_CONTRACTS_PATH,
    state_path: Path | None = None,
    repo_path: Path | None = REPO_ROOT,
    base_ref: str | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Bind an existing contracted task to a worker under the Codex control plane."""
    worker = _normalize_agent(worker)
    stage = str(stage or "").strip().lower()
    rationale = str(rationale or "").strip()
    if stage not in ORCHESTRATION_STAGES:
        raise CoordinationError(
            f"stage must be one of: {', '.join(sorted(ORCHESTRATION_STAGES))}"
        )
    if not rationale:
        raise CoordinationError("assignment rationale is required")

    state_path = Path(state_path or default_state_path())
    now = now or _now()
    if repo_path is not None:
        base_ref = _clean_repo_head(Path(repo_path))
    elif not base_ref:
        base_ref = "0" * 40
    with queue_state_lock(Path(queue_path)):
        queue = _load_json_list(Path(queue_path))
        state = _load_state(state_path)
        _expire_leases(queue, state, now)
        open_assignments = [
            item
            for item in queue
            if _task_id(item) != task_id
            and item.get("orchestrated_by") == CONTROL_PLANE_AGENT
            and item.get("orchestration_id")
            and item.get("status") not in {"cancelled", "done"}
        ]
        if open_assignments:
            raise CoordinationError(
                "another Codex assignment is open; complete or reject it before assignment"
            )
        task = next((item for item in queue if _task_id(item) == task_id), None)
        if task is None:
            raise CoordinationError(f"task not found: {task_id}")
        if task.get("status") not in ASSIGNABLE_STATUSES:
            raise CoordinationError(
                f"task {task_id} cannot be assigned from status {task.get('status')!r}"
            )
        if task.get("status") == "proposed" and not task.get("assigned_ai"):
            task["assigned_ai"] = worker

        contracts = load_contracts(Path(contracts_path))
        contract, spec, contract_digest, spec_digest = _contract_for_queue_task(
            task, contracts
        )
        parent_poc_digest = ""
        poc_required = bool(spec.constraints.get("poc_required", False))
        if poc_required and stage == "poc":
            raise CoordinationError("a POC task cannot itself require a parent POC")
        if poc_required and not parent_task_id:
            raise CoordinationError("this task requires an approved parent POC")
        if parent_task_id:
            task["orchestration_parent_task_id"] = str(parent_task_id).strip()
            parent_poc_digest = _validate_parent_poc(
                task,
                queue,
                contracts,
                require_assignment_binding=False,
            )

        orchestration_id = "orch_" + uuid.uuid4().hex[:16]
        task.update(
            {
                "status": (
                    "awaiting_approval"
                    if task.get("status") == "awaiting_approval"
                    else "queued"
                ),
                "assigned_to": worker,
                "assigned_at": _iso(now),
                "worker_type": worker,
                "orchestrated_by": CONTROL_PLANE_AGENT,
                "orchestration_id": orchestration_id,
                "orchestration_stage": stage,
                "orchestration_state": "assigned",
                "orchestration_rationale": rationale,
                "orchestration_assigned_at": _iso(now),
                "orchestration_contract_sha256": contract_digest,
                "orchestration_task_spec_sha256": spec_digest,
                "orchestration_base_ref": base_ref,
                "coordination_version": COORDINATION_VERSION,
            }
        )
        if parent_task_id:
            task["orchestration_parent_task_id"] = str(parent_task_id).strip()
            task["orchestration_parent_poc_sha256"] = parent_poc_digest
        else:
            task.pop("orchestration_parent_task_id", None)
            task.pop("orchestration_parent_poc_sha256", None)
        for field_name in (
            "blocked_at",
            "blocked_reason",
            "codex_reviewed_at",
            "codex_review_summary",
            "verification_failure_class",
            "verification_reasons",
        ):
            task.pop(field_name, None)
        state.update(
            {
                "control_plane": CONTROL_PLANE_AGENT,
                "last_assignment_id": orchestration_id,
                "last_assigned_task": task_id,
                "last_assignment_at": _iso(now),
            }
        )
        if repo_path is not None and _clean_repo_head(Path(repo_path)) != base_ref:
            raise CoordinationError("repository HEAD changed during Codex assignment")
        _atomic_write_json(Path(queue_path), queue)
        _atomic_write_json(state_path, state)
    return {
        "status": "assigned",
        "controller": CONTROL_PLANE_AGENT,
        "worker": worker,
        "task_id": task_id,
        "stage": stage,
        "orchestration_id": orchestration_id,
        "contract_sha256": contract_digest,
        "task_spec_sha256": spec_digest,
        "base_ref": base_ref,
    }


def set_cooldown(
    agent: str,
    *,
    seconds: int,
    reason: str,
    queue_path: Path = WORK_QUEUE_PATH,
    state_path: Path | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Mark an agent unavailable and release any lease it currently owns."""
    agent = _normalize_agent(agent)
    if int(seconds) <= 0:
        raise CoordinationError("cooldown seconds must be greater than zero")
    state_path = Path(state_path or default_state_path())
    now = now or _now()
    released: list[str] = []
    with queue_state_lock(Path(queue_path)):
        queue = _load_json_list(Path(queue_path))
        state = _load_state(state_path)
        for task in queue:
            if (
                task.get("status") in ACTIVE_STATUSES
                and _assigned_agent(task.get("lease_owner")) == agent
            ):
                released.append(_task_id(task))
                _clear_lease(task, restore_assignee=True)
                task["status"] = "queued"
                task["orchestration_state"] = "assigned"
                task["released_at"] = _iso(now)
                task["handoff_reason"] = reason or f"{agent} entered cooldown"
        until = now + dt.timedelta(seconds=int(seconds))
        _agent_record(state, agent).update(
            {
                "status": "cooldown",
                "cooldown_until": _iso(until),
                "cooldown_reason": str(reason or "rate limit").strip(),
                "updated_at": _iso(now),
            }
        )
        _atomic_write_json(Path(queue_path), queue)
        _atomic_write_json(state_path, state)
    return {
        "status": "cooldown",
        "agent": agent,
        "cooldown_until": _iso(until),
        "released_tasks": released,
    }


def clear_cooldown(
    agent: str,
    *,
    queue_path: Path = WORK_QUEUE_PATH,
    state_path: Path | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    agent = _normalize_agent(agent)
    state_path = Path(state_path or default_state_path())
    now = now or _now()
    with queue_state_lock(Path(queue_path)):
        state = _load_state(state_path)
        record = _agent_record(state, agent)
        record.update({"status": "available", "updated_at": _iso(now)})
        record.pop("cooldown_until", None)
        record.pop("cooldown_reason", None)
        _atomic_write_json(state_path, state)
    return {"status": "available", "agent": agent}


def claim_next(
    agent: str,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    queue_path: Path = WORK_QUEUE_PATH,
    contracts_path: Path = TASK_CONTRACTS_PATH,
    approvals_path: Path = APPROVED_TASKS_PATH,
    state_path: Path | None = None,
    repo_path: Path | None = REPO_ROOT,
    base_ref: str | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Claim the highest-priority task explicitly assigned by Codex."""
    agent = _normalize_agent(agent)
    if int(lease_seconds) <= 0:
        raise CoordinationError("lease seconds must be greater than zero")
    state_path = Path(state_path or default_state_path())
    now = now or _now()
    if repo_path is not None:
        base_ref = _clean_repo_head(Path(repo_path))
    elif not base_ref:
        base_ref = "0" * 40

    with queue_state_lock(Path(queue_path)):
        queue = _load_json_list(Path(queue_path))
        state = _load_state(state_path)
        expired = _expire_leases(queue, state, now)
        cooldown_until = _cooldown_until(state, agent, now)
        if cooldown_until is not None:
            _atomic_write_json(Path(queue_path), queue)
            _atomic_write_json(state_path, state)
            return {
                "status": "cooldown",
                "agent": agent,
                "cooldown_until": _iso(cooldown_until),
                "expired_leases": expired,
            }

        active = [task for task in queue if task.get("status") in ACTIVE_STATUSES]
        if active:
            _atomic_write_json(Path(queue_path), queue)
            _atomic_write_json(state_path, state)
            return {
                "status": "capacity",
                "agent": agent,
                "active": len(active),
                "task_ids": [_task_id(task) for task in active],
                "expired_leases": expired,
            }

        contracts = load_contracts(Path(contracts_path))
        candidates = sorted(
            enumerate(queue),
            key=lambda item: (
                item[1].get("priority", 99),
                str(item[1].get("created_at") or ""),
                item[0],
            ),
        )
        selected: tuple[int, TaskContract, TaskSpec, str, str] | None = None
        queue_changed = bool(expired)
        skipped: list[dict[str, str]] = []

        for index, task in candidates:
            if task.get("status") not in {"queued", "awaiting_approval"}:
                continue
            task_id = _task_id(task)
            if task.get("blocked_reason") and task.get("status") == "queued":
                skipped.append({"task_id": task_id, "reason": "blocked precondition"})
                continue
            if not _codex_assignment_allows_claim(task, agent):
                skipped.append({
                    "task_id": task_id,
                    "reason": "not assigned by Codex to this worker",
                })
                continue
            try:
                contract, spec, contract_digest, spec_digest = _contract_for_queue_task(
                    task, contracts
                )
            except CoordinationError as exc:
                task["status"] = "blocked"
                task["blocked_reason"] = str(exc)
                task["blocked_at"] = _iso(now)
                queue_changed = True
                skipped.append({"task_id": task_id, "reason": str(exc)})
                continue
            if (
                task.get("orchestration_contract_sha256") != contract_digest
                or task.get("orchestration_task_spec_sha256") != spec_digest
                or task.get("orchestration_base_ref") != base_ref
            ):
                task["status"] = "blocked"
                task["blocked_reason"] = (
                    "Codex assignment no longer matches task, contract, or base commit"
                )
                task["blocked_at"] = _iso(now)
                task["orchestration_state"] = "invalidated"
                queue_changed = True
                skipped.append({
                    "task_id": task_id,
                    "reason": "Codex assignment digest mismatch",
                })
                continue
            parent_task_id = str(
                task.get("orchestration_parent_task_id") or ""
            ).strip()
            if parent_task_id:
                try:
                    _validate_parent_poc(
                        task,
                        queue,
                        contracts,
                        require_assignment_binding=True,
                    )
                except CoordinationError:
                    task["status"] = "blocked"
                    task["blocked_reason"] = "parent POC approval changed after assignment"
                    task["blocked_at"] = _iso(now)
                    task["orchestration_state"] = "invalidated"
                    queue_changed = True
                    skipped.append({
                        "task_id": task_id,
                        "reason": "parent POC approval mismatch",
                    })
                    continue
            approved = approval_logged(
                contract.task_id,
                Path(approvals_path),
                task_contract_sha256=contract_digest,
                task_spec_sha256=spec_digest,
            )
            if contract.requires_approval and not approved:
                task["status"] = "awaiting_approval"
                task["blocked_reason"] = "requires digest-bound human approval"
                task["blocked_at"] = _iso(now)
                queue_changed = True
                skipped.append({"task_id": task_id, "reason": "awaiting approval"})
                continue
            selected = (index, contract, spec, contract_digest, spec_digest)
            break

        if selected is None:
            if queue_changed:
                _atomic_write_json(Path(queue_path), queue)
            _atomic_write_json(state_path, state)
            return {
                "status": "idle",
                "agent": agent,
                "expired_leases": expired,
                "skipped": skipped,
            }

        index, contract, spec, contract_digest, spec_digest = selected
        task = queue[index]
        consumed_approval: dict[str, Any] | None = None
        if contract.requires_approval:
            consumed_approval = consume_approval(
                contract.task_id,
                task_contract_sha256=contract_digest,
                task_spec_sha256=spec_digest,
                approvals_path=Path(approvals_path),
            )
            if consumed_approval is None:
                task["status"] = "awaiting_approval"
                task["blocked_reason"] = "approval was already consumed"
                task["blocked_at"] = _iso(now)
                _atomic_write_json(Path(queue_path), queue)
                _atomic_write_json(state_path, state)
                return {
                    "status": "idle",
                    "agent": agent,
                    "skipped": [{"task_id": _task_id(task), "reason": "approval consumed"}],
                }

        if repo_path is not None and _clean_repo_head(Path(repo_path)) != base_ref:
            if consumed_approval is not None:
                restore_approval(
                    consumed_approval,
                    approvals_path=Path(approvals_path),
                )
            raise CoordinationError("repository HEAD changed during task claim")

        lease_id = "lease_" + uuid.uuid4().hex[:16]
        expires_at = now + dt.timedelta(seconds=int(lease_seconds))
        previous_assignee = task.get("assigned_to")
        task.update(
            {
                "status": "in_progress",
                "assigned_to": agent,
                "assigned_at": _iso(now),
                "lease_owner": agent,
                "lease_id": lease_id,
                "lease_acquired_at": _iso(now),
                "lease_expires_at": _iso(expires_at),
                "lease_base_ref": base_ref,
                "lease_previous_assignee": previous_assignee,
                "lease_contract_sha256": contract_digest,
                "lease_task_spec_sha256": spec_digest,
                "coordination_version": COORDINATION_VERSION,
                "contract_validated_at": _iso(now),
                "contract_version": contract.contract_version,
                "orchestration_state": "leased",
            }
        )
        task.pop("blocked_reason", None)
        task.pop("blocked_at", None)
        record = _agent_record(state, agent)
        record.update(
            {
                "status": "busy",
                "task_id": _task_id(task),
                "lease_id": lease_id,
                "updated_at": _iso(now),
            }
        )
        state["last_claimed_agent"] = agent
        state["last_claimed_at"] = _iso(now)
        try:
            _atomic_write_json(Path(queue_path), queue)
        except Exception:
            if consumed_approval is not None:
                restore_approval(
                    consumed_approval,
                    approvals_path=Path(approvals_path),
                )
            raise
        _atomic_write_json(state_path, state)
        return {
            "status": "claimed",
            "agent": agent,
            "task_id": _task_id(task),
            "lease_id": lease_id,
            "lease_expires_at": _iso(expires_at),
            "base_ref": base_ref,
            "task": copy.deepcopy(task),
            "skipped": skipped,
            "expired_leases": expired,
        }


def heartbeat(
    agent: str,
    task_id: str,
    lease_id: str,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    queue_path: Path = WORK_QUEUE_PATH,
    state_path: Path | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    agent = _normalize_agent(agent)
    if int(lease_seconds) <= 0:
        raise CoordinationError("lease seconds must be greater than zero")
    state_path = Path(state_path or default_state_path())
    now = now or _now()
    with queue_state_lock(Path(queue_path)):
        queue = _load_json_list(Path(queue_path))
        task = next((item for item in queue if _task_id(item) == task_id), None)
        if task is None:
            raise CoordinationError(f"task not found: {task_id}")
        if task.get("lease_owner") != agent or task.get("lease_id") != lease_id:
            raise CoordinationError("lease ownership does not match")
        if task.get("status") != "in_progress" or not _valid_v2_lease_shape(
            task, agent
        ):
            raise CoordinationError("task does not have a valid coordination v2 lease")
        current_expiry = _parse_time(task.get("lease_expires_at"))
        if current_expiry is None or current_expiry <= now:
            raise CoordinationError("lease expired before heartbeat")
        expires_at = now + dt.timedelta(seconds=int(lease_seconds))
        task["lease_expires_at"] = _iso(expires_at)
        task["last_heartbeat_at"] = _iso(now)
        state = _load_state(state_path)
        _agent_record(state, agent).update(
            {
                "status": "busy",
                "task_id": task_id,
                "lease_id": lease_id,
                "updated_at": _iso(now),
            }
        )
        _atomic_write_json(Path(queue_path), queue)
        _atomic_write_json(state_path, state)
    return {
        "status": "renewed",
        "agent": agent,
        "task_id": task_id,
        "lease_id": lease_id,
        "lease_expires_at": _iso(expires_at),
    }


def release(
    agent: str,
    task_id: str,
    lease_id: str,
    *,
    reason: str,
    queue_path: Path = WORK_QUEUE_PATH,
    state_path: Path | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    agent = _normalize_agent(agent)
    state_path = Path(state_path or default_state_path())
    now = now or _now()
    with queue_state_lock(Path(queue_path)):
        queue = _load_json_list(Path(queue_path))
        task = next((item for item in queue if _task_id(item) == task_id), None)
        if task is None:
            raise CoordinationError(f"task not found: {task_id}")
        if task.get("lease_owner") != agent or task.get("lease_id") != lease_id:
            raise CoordinationError("lease ownership does not match")
        if task.get("status") != "in_progress" or not _valid_v2_lease_shape(
            task, agent
        ):
            raise CoordinationError("task does not have a valid coordination v2 lease")
        _clear_lease(task, restore_assignee=True)
        task["status"] = "queued"
        task["orchestration_state"] = "assigned"
        task["released_at"] = _iso(now)
        task["handoff_reason"] = str(reason or "released").strip()
        state = _load_state(state_path)
        _agent_record(state, agent).update(
            {"status": "available", "updated_at": _iso(now)}
        )
        _atomic_write_json(Path(queue_path), queue)
        _atomic_write_json(state_path, state)
    return {"status": "released", "agent": agent, "task_id": task_id}


def _compact_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    return compact_completion_evidence(evidence)


def finish(
    agent: str,
    task_id: str,
    lease_id: str,
    *,
    summary: str,
    queue_path: Path = WORK_QUEUE_PATH,
    contracts_path: Path = TASK_CONTRACTS_PATH,
    state_path: Path | None = None,
    repo_path: Path = REPO_ROOT,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Collect loop-owned evidence and close a lease only when it verifies."""
    agent = _normalize_agent(agent)
    state_path = Path(state_path or default_state_path())
    now_was_provided = now is not None
    now = now or _now()
    head = _clean_repo_head(Path(repo_path))

    with queue_state_lock(Path(queue_path)):
        queue = _load_json_list(Path(queue_path))
        task = next((item for item in queue if _task_id(item) == task_id), None)
        if task is None:
            raise CoordinationError(f"task not found: {task_id}")
        if task.get("lease_owner") != agent or task.get("lease_id") != lease_id:
            raise CoordinationError("lease ownership does not match")
        if task.get("status") != "in_progress":
            raise CoordinationError("task is not in progress")
        if not _valid_v2_lease_shape(task, agent):
            raise CoordinationError("task does not have a valid coordination v2 lease")
        lease_expiry = _parse_time(task.get("lease_expires_at"))
        if lease_expiry is None or lease_expiry <= now:
            raise CoordinationError("lease expired before completion verification")
        base_ref = str(task.get("lease_base_ref") or "")
        contracts = load_contracts(Path(contracts_path))
        contract, spec, contract_digest, spec_digest = _contract_for_queue_task(
            task, contracts
        )
        _validate_parent_poc(
            task,
            queue,
            contracts,
            require_assignment_binding=True,
        )
        if (
            task.get("lease_contract_sha256") != contract_digest
            or task.get("lease_task_spec_sha256") != spec_digest
        ):
            raise CoordinationError("task contract changed after the lease was claimed")
        leased_contract_digest = str(task.get("lease_contract_sha256") or "")
        leased_spec_digest = str(task.get("lease_task_spec_sha256") or "")
        verification_deadline = now + dt.timedelta(
            seconds=spec.budget.wall_time_seconds + 60
        )
        task["lease_expires_at"] = _iso(verification_deadline)
        _atomic_write_json(Path(queue_path), queue)

    try:
        assessment = verify_completion(
            spec,
            Path(repo_path),
            base_ref,
            completion_ref=head,
        )
        evidence = dict(assessment.evidence)
        verdict = assessment.verdict
    except CompletionEvidenceError as exc:
        evidence = {"observer": "loop", "error": str(exc)}
        verdict_status = "unverified"
        failure_class = "infrastructure_failure"
        reasons = (str(exc),)
    else:
        if not assessment.gate.passed:
            if assessment.gate.infrastructure_failed:
                verdict_status = "unverified"
                failure_class = "infrastructure_failure"
            else:
                verdict_status = "needs_review"
                failure_class = "pre_commit_gate_violation"
            reasons = tuple(assessment.gate.reasons())
        else:
            verdict_status = verdict.status
            failure_class = verdict.failure_class
            reasons = verdict.reasons

    try:
        current_head = _clean_repo_head(Path(repo_path))
    except CoordinationError as exc:
        verdict_status = "unverified"
        failure_class = "infrastructure_failure"
        reasons = (str(exc),)
    else:
        if current_head != head:
            verdict_status = "unverified"
            failure_class = "infrastructure_failure"
            reasons = ("repository HEAD changed during completion verification",)

    finished_at = now if now_was_provided else _now()
    with queue_state_lock(Path(queue_path)):
        queue = _load_json_list(Path(queue_path))
        task = next((item for item in queue if _task_id(item) == task_id), None)
        if task is None:
            raise CoordinationError(f"task disappeared during verification: {task_id}")
        if task.get("lease_owner") != agent or task.get("lease_id") != lease_id:
            raise CoordinationError("lease changed during verification")
        if task.get("status") != "in_progress":
            raise CoordinationError("task status changed during verification")
        if str(task.get("lease_base_ref") or "") != base_ref:
            raise CoordinationError("task base commit changed during verification")
        final_expiry = _parse_time(task.get("lease_expires_at"))
        if final_expiry is None or final_expiry <= finished_at:
            raise CoordinationError("lease expired during completion verification")
        if (
            task.get("lease_contract_sha256") != leased_contract_digest
            or task.get("lease_task_spec_sha256") != leased_spec_digest
        ):
            raise CoordinationError("lease digests changed during verification")
        contracts = load_contracts(Path(contracts_path))
        _, _, current_contract_digest, current_spec_digest = _contract_for_queue_task(
            task, contracts
        )
        _validate_parent_poc(
            task,
            queue,
            contracts,
            require_assignment_binding=True,
        )
        if (
            leased_contract_digest != current_contract_digest
            or leased_spec_digest != current_spec_digest
        ):
            raise CoordinationError("task contract changed during verification")
        if _clean_repo_head(Path(repo_path)) != head:
            raise CoordinationError("repository HEAD changed before queue promotion")
        passed = verdict_status == "verified"
        if passed:
            task.update(
                {
                    "status": CODEX_REVIEW_STATUS,
                    "candidate_completed_at": _iso(finished_at),
                    "candidate_completed_by": agent,
                    "executed_by": agent,
                    "candidate_result_summary": str(
                        summary or "verified completion"
                    ).strip(),
                    "completion_commit": head,
                    "completion_evidence": _compact_evidence(evidence),
                    "orchestration_state": CODEX_REVIEW_STATUS,
                }
            )
            _clear_lease(task, restore_assignee=False)
        else:
            previous = task.get("lease_previous_assignee")
            _clear_lease(task, restore_assignee=False)
            task.update(
                {
                    "status": (
                        "unverified"
                        if verdict_status == "unverified"
                        else "needs_review"
                        if verdict_status == "needs_review"
                        else "blocked"
                    ),
                    "assigned_to": previous,
                    "assigned_at": None,
                    "verification_failure_class": failure_class,
                    "verification_reasons": list(reasons),
                    "completion_evidence": _compact_evidence(evidence),
                    "completion_commit": head,
                    "verified_at": _iso(now),
                    "orchestration_state": "verification_failed",
                }
            )
        state = _load_state(state_path)
        record = _agent_record(state, agent)
        record.update(
            {
                "status": "finalizing",
                "updated_at": _iso(finished_at),
                "pending_completion": {
                    "task_id": task_id,
                    "lease_id": lease_id,
                    "completion_commit": head,
                    "verdict_status": verdict_status,
                    "failure_class": failure_class,
                    "reasons": list(reasons),
                    "evidence": _compact_evidence(evidence),
                },
            }
        )
        _atomic_write_json(state_path, state)
        _atomic_write_json(Path(queue_path), queue)
        record.update({"status": "available", "updated_at": _iso(finished_at)})
        record.pop("task_id", None)
        record.pop("lease_id", None)
        record.pop("pending_completion", None)
        try:
            _atomic_write_json(state_path, state)
        except OSError:
            log.exception(
                "queue promotion committed but agent state cleanup failed for %s",
                task_id,
            )
    result_status = CODEX_REVIEW_STATUS if passed else verdict_status
    return {
        "status": result_status,
        "agent": agent,
        "task_id": task_id,
        "commit": head,
        "failure_class": failure_class,
        "reasons": list(reasons),
        "evidence": _compact_evidence(evidence),
    }


def review_completion(
    task_id: str,
    *,
    decision: str,
    summary: str,
    queue_path: Path = WORK_QUEUE_PATH,
    contracts_path: Path = TASK_CONTRACTS_PATH,
    state_path: Path | None = None,
    repo_path: Path = REPO_ROOT,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Accept or reject a delegated completion as the Codex control plane."""
    decision = str(decision or "").strip().lower()
    summary = str(summary or "").strip()
    if decision not in {"accept", "reject"}:
        raise CoordinationError("decision must be 'accept' or 'reject'")
    if not summary:
        raise CoordinationError("Codex review summary is required")
    state_path = Path(state_path or default_state_path())
    now = now or _now()
    head = _clean_repo_head(Path(repo_path))

    with queue_state_lock(Path(queue_path)):
        queue = _load_json_list(Path(queue_path))
        task = next((item for item in queue if _task_id(item) == task_id), None)
        if task is None:
            raise CoordinationError(f"task not found: {task_id}")
        if task.get("status") != CODEX_REVIEW_STATUS:
            raise CoordinationError(f"task {task_id} is not awaiting Codex review")
        if task.get("orchestrated_by") != CONTROL_PLANE_AGENT:
            raise CoordinationError("task was not assigned by the Codex control plane")
        if str(task.get("completion_commit") or "") != head:
            raise CoordinationError("reviewed completion is not the current clean HEAD")

        contracts = load_contracts(Path(contracts_path))
        _, _, contract_digest, spec_digest = _contract_for_queue_task(task, contracts)
        _validate_parent_poc(
            task,
            queue,
            contracts,
            require_assignment_binding=True,
        )
        if (
            task.get("orchestration_contract_sha256") != contract_digest
            or task.get("orchestration_task_spec_sha256") != spec_digest
        ):
            raise CoordinationError("task or contract changed after Codex assignment")

        task["codex_reviewed_at"] = _iso(now)
        task["codex_reviewed_by"] = CONTROL_PLANE_AGENT
        task["codex_review_summary"] = summary
        if decision == "accept":
            task.update(
                {
                    "status": "done",
                    "completed_at": _iso(now),
                    "completed_by": CONTROL_PLANE_AGENT,
                    "result_summary": task.get("candidate_result_summary") or summary,
                    "orchestration_state": "completed",
                }
            )
            if task.get("orchestration_stage") == "poc":
                task.update(
                    {
                        "poc_approved_at": _iso(now),
                        "poc_approved_by": CONTROL_PLANE_AGENT,
                        "poc_approval_sha256": _poc_approval_digest(
                            task_id,
                            contract_digest,
                            spec_digest,
                            head,
                        ),
                    }
                )
        else:
            task.update(
                {
                    "status": "needs_review",
                    "assigned_to": None,
                    "assigned_at": None,
                    "orchestration_state": "review_rejected",
                    "verification_failure_class": "codex_review_rejected",
                    "verification_reasons": [summary],
                }
            )

        state = _load_state(state_path)
        state.update(
            {
                "control_plane": CONTROL_PLANE_AGENT,
                "last_reviewed_task": task_id,
                "last_review_decision": decision,
                "last_review_at": _iso(now),
            }
        )
        if _clean_repo_head(Path(repo_path)) != head:
            raise CoordinationError("repository HEAD changed during Codex review")
        _atomic_write_json(Path(queue_path), queue)
        _atomic_write_json(state_path, state)
    return {
        "status": "done" if decision == "accept" else "needs_review",
        "controller": CONTROL_PLANE_AGENT,
        "task_id": task_id,
        "decision": decision,
        "commit": head,
    }


def status_snapshot(
    *,
    queue_path: Path = WORK_QUEUE_PATH,
    state_path: Path | None = None,
) -> dict[str, Any]:
    state_path = Path(state_path or default_state_path())
    with queue_state_lock(Path(queue_path)):
        queue = _load_json_list(Path(queue_path))
        state = _load_state(state_path)
    counts: dict[str, int] = {}
    for task in queue:
        status = str(task.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "coordination": state,
        "queue_counts": counts,
        "active_leases": [
            {
                "task_id": _task_id(task),
                "agent": task.get("lease_owner"),
                "lease_id": task.get("lease_id"),
                "lease_expires_at": task.get("lease_expires_at"),
            }
            for task in queue
            if task.get("status") in ACTIVE_STATUSES
        ],
    }


def _emit(payload: Mapping[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(dict(payload), ensure_ascii=False))
        return
    print(json.dumps(dict(payload), indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Codex-controlled Jarvis engineering queue."
    )
    parser.add_argument("--queue-path", type=Path, default=WORK_QUEUE_PATH)
    parser.add_argument("--contracts-path", type=Path, default=TASK_CONTRACTS_PATH)
    parser.add_argument("--approvals-path", type=Path, default=APPROVED_TASKS_PATH)
    parser.add_argument("--state-path", type=Path, default=None)
    parser.add_argument("--repo-path", type=Path, default=REPO_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    assign_parser = subparsers.add_parser("assign")
    assign_parser.add_argument("--task-id", required=True)
    assign_parser.add_argument("--worker", required=True, choices=sorted(SUPPORTED_AGENTS))
    assign_parser.add_argument("--stage", required=True, choices=sorted(ORCHESTRATION_STAGES))
    assign_parser.add_argument("--rationale", required=True)
    assign_parser.add_argument("--parent-task-id", default="")
    assign_parser.add_argument("--json", action="store_true")

    claim_parser = subparsers.add_parser("claim")
    claim_parser.add_argument("--agent", required=True, choices=sorted(SUPPORTED_AGENTS))
    claim_parser.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS)
    claim_parser.add_argument("--json", action="store_true")

    cooldown_parser = subparsers.add_parser("cooldown")
    cooldown_parser.add_argument("--agent", required=True, choices=sorted(SUPPORTED_AGENTS))
    cooldown_parser.add_argument("--seconds", required=True, type=int)
    cooldown_parser.add_argument("--reason", default="rate limit")
    cooldown_parser.add_argument("--json", action="store_true")

    clear_parser = subparsers.add_parser("clear-cooldown")
    clear_parser.add_argument("--agent", required=True, choices=sorted(SUPPORTED_AGENTS))
    clear_parser.add_argument("--json", action="store_true")

    heartbeat_parser = subparsers.add_parser("heartbeat")
    heartbeat_parser.add_argument("--agent", required=True, choices=sorted(SUPPORTED_AGENTS))
    heartbeat_parser.add_argument("--task-id", required=True)
    heartbeat_parser.add_argument("--lease-id", required=True)
    heartbeat_parser.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS)
    heartbeat_parser.add_argument("--json", action="store_true")

    release_parser = subparsers.add_parser("release")
    release_parser.add_argument("--agent", required=True, choices=sorted(SUPPORTED_AGENTS))
    release_parser.add_argument("--task-id", required=True)
    release_parser.add_argument("--lease-id", required=True)
    release_parser.add_argument("--reason", required=True)
    release_parser.add_argument("--json", action="store_true")

    finish_parser = subparsers.add_parser("finish")
    finish_parser.add_argument("--agent", required=True, choices=sorted(SUPPORTED_AGENTS))
    finish_parser.add_argument("--task-id", required=True)
    finish_parser.add_argument("--lease-id", required=True)
    finish_parser.add_argument("--summary", required=True)
    finish_parser.add_argument("--json", action="store_true")

    review_parser = subparsers.add_parser("review")
    review_parser.add_argument("--task-id", required=True)
    review_parser.add_argument("--decision", required=True, choices=("accept", "reject"))
    review_parser.add_argument("--summary", required=True)
    review_parser.add_argument("--json", action="store_true")

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    common = {
        "queue_path": args.queue_path,
        "state_path": args.state_path,
    }
    try:
        if args.command == "assign":
            payload = assign_task(
                args.task_id,
                args.worker,
                stage=args.stage,
                rationale=args.rationale,
                parent_task_id=args.parent_task_id,
                contracts_path=args.contracts_path,
                repo_path=args.repo_path,
                **common,
            )
        elif args.command == "claim":
            payload = claim_next(
                args.agent,
                lease_seconds=args.lease_seconds,
                contracts_path=args.contracts_path,
                approvals_path=args.approvals_path,
                repo_path=args.repo_path,
                **common,
            )
        elif args.command == "cooldown":
            payload = set_cooldown(
                args.agent,
                seconds=args.seconds,
                reason=args.reason,
                **common,
            )
        elif args.command == "clear-cooldown":
            payload = clear_cooldown(args.agent, **common)
        elif args.command == "heartbeat":
            payload = heartbeat(
                args.agent,
                args.task_id,
                args.lease_id,
                lease_seconds=args.lease_seconds,
                **common,
            )
        elif args.command == "release":
            payload = release(
                args.agent,
                args.task_id,
                args.lease_id,
                reason=args.reason,
                **common,
            )
        elif args.command == "finish":
            payload = finish(
                args.agent,
                args.task_id,
                args.lease_id,
                summary=args.summary,
                contracts_path=args.contracts_path,
                repo_path=args.repo_path,
                **common,
            )
        elif args.command == "review":
            payload = review_completion(
                args.task_id,
                decision=args.decision,
                summary=args.summary,
                contracts_path=args.contracts_path,
                repo_path=args.repo_path,
                **common,
            )
        else:
            payload = status_snapshot(**common)
    except CoordinationError as exc:
        _emit({"status": "error", "error": str(exc)}, getattr(args, "json", False))
        return 1
    _emit(payload, getattr(args, "json", False))
    return 0 if payload.get("status") not in {"blocked", "rejected", "unverified"} else 1


if __name__ == "__main__":
    sys.exit(main())
