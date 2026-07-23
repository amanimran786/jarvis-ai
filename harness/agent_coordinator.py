"""Atomic Claude/Codex task leasing for the shared Jarvis checkout.

The coordinator deliberately permits one active engineering lease in the
shared checkout. Parallel agents require isolated worktrees and a separate
merge arbiter; counting two agents in one directory is not safe concurrency.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
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
COORDINATION_VERSION = 1
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
    if "codex" in text:
        return "codex"
    if "claude" in text or "cowork" in text:
        return "claude"
    if "gemini" in text:
        return "gemini"
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
    try:
        result = subprocess.run(
            ["git", *args],
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
        if owner not in SUPPORTED_AGENTS or expires_at is None or expires_at > now:
            continue
        task_id = _task_id(task)
        _clear_lease(task, restore_assignee=True)
        task["status"] = "queued"
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


def _owner_allows_claim(
    task: Mapping[str, Any],
    agent: str,
    state: Mapping[str, Any],
    now: dt.datetime,
    takeover_cooling: bool,
) -> bool:
    owner = _assigned_agent(task.get("assigned_to"))
    if owner is None or owner == agent:
        return True
    return bool(
        takeover_cooling
        and owner in SUPPORTED_AGENTS
        and _cooldown_until(state, owner, now) is not None
    )


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
    takeover_cooling: bool = False,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    queue_path: Path = WORK_QUEUE_PATH,
    contracts_path: Path = TASK_CONTRACTS_PATH,
    approvals_path: Path = APPROVED_TASKS_PATH,
    state_path: Path | None = None,
    repo_path: Path | None = REPO_ROOT,
    base_ref: str | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Atomically claim the highest-priority task eligible for ``agent``."""
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
            if not _owner_allows_claim(
                task, agent, state, now, takeover_cooling
            ):
                skipped.append({"task_id": task_id, "reason": "assigned to another agent"})
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
    state_path = Path(state_path or default_state_path())
    now = now or _now()
    with queue_state_lock(Path(queue_path)):
        queue = _load_json_list(Path(queue_path))
        task = next((item for item in queue if _task_id(item) == task_id), None)
        if task is None:
            raise CoordinationError(f"task not found: {task_id}")
        if task.get("lease_owner") != agent or task.get("lease_id") != lease_id:
            raise CoordinationError("lease ownership does not match")
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
        _clear_lease(task, restore_assignee=True)
        task["status"] = "queued"
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
        lease_expiry = _parse_time(task.get("lease_expires_at"))
        if lease_expiry is None or lease_expiry <= now:
            raise CoordinationError("lease expired before completion verification")
        base_ref = str(task.get("lease_base_ref") or "")
        contracts = load_contracts(Path(contracts_path))
        contract, spec, contract_digest, spec_digest = _contract_for_queue_task(
            task, contracts
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
                    "status": "done",
                    "completed_at": _iso(now),
                    "completed_by": agent,
                    "result_summary": str(summary or "verified completion").strip(),
                    "completion_commit": head,
                    "completion_evidence": _compact_evidence(evidence),
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
    return {
        "status": "verified" if passed else verdict_status,
        "agent": agent,
        "task_id": task_id,
        "commit": head,
        "failure_class": failure_class,
        "reasons": list(reasons),
        "evidence": _compact_evidence(evidence),
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
        description="Coordinate Claude and Codex over the Jarvis work queue."
    )
    parser.add_argument("--queue-path", type=Path, default=WORK_QUEUE_PATH)
    parser.add_argument("--contracts-path", type=Path, default=TASK_CONTRACTS_PATH)
    parser.add_argument("--approvals-path", type=Path, default=APPROVED_TASKS_PATH)
    parser.add_argument("--state-path", type=Path, default=None)
    parser.add_argument("--repo-path", type=Path, default=REPO_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    claim_parser = subparsers.add_parser("claim")
    claim_parser.add_argument("--agent", required=True, choices=sorted(SUPPORTED_AGENTS))
    claim_parser.add_argument("--takeover-cooling", action="store_true")
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
        if args.command == "claim":
            payload = claim_next(
                args.agent,
                takeover_cooling=args.takeover_cooling,
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
        else:
            payload = status_snapshot(**common)
    except CoordinationError as exc:
        _emit({"status": "error", "error": str(exc)}, getattr(args, "json", False))
        return 1
    _emit(payload, getattr(args, "json", False))
    return 0 if payload.get("status") not in {"blocked", "rejected", "unverified"} else 1


if __name__ == "__main__":
    sys.exit(main())
