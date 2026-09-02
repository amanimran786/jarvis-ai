"""Human approval records for contract-gated orchestration tasks."""

from __future__ import annotations

import datetime as _datetime
import fcntl
import getpass
import json
import os
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from harness.state_lock import queue_state_lock
from harness.task_contract import (
    APPROVED_TASKS_PATH,
    TASK_CONTRACTS_PATH,
    ContractError,
    TaskSpec,
    approval_logged,
    contract_for_task,
    is_sha256_digest,
    load_contracts,
    normalize_task_id,
    normalized_task_spec_digest,
    task_contract_digest,
)


WORK_QUEUE_PATH = Path(__file__).resolve().parent.parent / "WORK_QUEUE.json"


class ApprovalWorkflowError(ValueError):
    """Raised when approval state cannot be validated or safely updated."""


def _load_json_list(path: Path, *, missing_ok: bool = False) -> list[Any]:
    path = Path(path)
    if missing_ok and not path.exists():
        return []
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ApprovalWorkflowError(f"could not read {path}: {exc}") from exc
    if not isinstance(data, list):
        raise ApprovalWorkflowError(f"{path} must contain a JSON list")
    return data


def _approval_records(path: Path) -> list[dict[str, Any]]:
    records = _load_json_list(path, missing_ok=True)
    normalized: list[dict[str, Any]] = []
    for entry in records:
        if not isinstance(entry, Mapping):
            raise ApprovalWorkflowError(
                f"{path} contains an approval record that is not an object"
            )
        missing = [
            field_name
            for field_name in ("task_id", "approved_at", "approved_by")
            if not str(entry.get(field_name) or "").strip()
        ]
        if missing:
            raise ApprovalWorkflowError(
                f"{path} contains an approval record missing: {', '.join(missing)}"
            )
        record = dict(entry)
        try:
            record["task_id"] = normalize_task_id(record["task_id"])
        except ContractError as exc:
            raise ApprovalWorkflowError(f"{path} contains invalid approval task_id") from exc
        normalized.append(record)
    return normalized


def _approval_key(record: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(record.get("task_id") or "").strip(),
        str(record.get("task_contract_sha256") or "").strip(),
        str(record.get("task_spec_sha256") or "").strip(),
    )


def _deduplicate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduplicated: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for record in records:
        key = _approval_key(record)
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(record)
    return deduplicated


@contextmanager
def _approval_lock(path: Path) -> Iterator[None]:
    lock_path = Path(path).with_name(f".{Path(path).name}.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _atomic_write(path: Path, records: list[dict[str, Any]]) -> None:
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(records, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def record_approval(
    task_id: str,
    *,
    approved_by: str | None = None,
    approved_at: str | None = None,
    task_spec: TaskSpec | Mapping[str, Any] | None = None,
    queue_path: Path = WORK_QUEUE_PATH,
    contracts_path: Path = TASK_CONTRACTS_PATH,
    approvals_path: Path = APPROVED_TASKS_PATH,
) -> tuple[dict[str, Any], bool]:
    """Record one digest-bound approval; return ``(record, created)``."""
    with queue_state_lock(Path(queue_path)):
        return _record_approval_locked(
            task_id,
            approved_by=approved_by,
            approved_at=approved_at,
            task_spec=task_spec,
            queue_path=queue_path,
            contracts_path=contracts_path,
            approvals_path=approvals_path,
        )


def _record_approval_locked(
    task_id: str,
    *,
    approved_by: str | None = None,
    approved_at: str | None = None,
    task_spec: TaskSpec | Mapping[str, Any] | None = None,
    queue_path: Path = WORK_QUEUE_PATH,
    contracts_path: Path = TASK_CONTRACTS_PATH,
    approvals_path: Path = APPROVED_TASKS_PATH,
) -> tuple[dict[str, Any], bool]:
    """Record an approval while the caller holds the queue-state lock."""
    try:
        normalized_id = normalize_task_id(task_id)
    except ContractError as exc:
        raise ApprovalWorkflowError(str(exc)) from exc
    contract = contract_for_task(normalized_id, Path(contracts_path))
    if contract is None:
        raise ApprovalWorkflowError(f"no task contract found for: {normalized_id or task_id}")

    if task_spec is None:
        matching_tasks = [
            task
            for task in _load_json_list(Path(queue_path), missing_ok=True)
            if isinstance(task, Mapping) and _queue_task_id(task) == normalized_id
        ]
        if len(matching_tasks) != 1:
            raise ApprovalWorkflowError(
                f"expected exactly one executable queue task for: {normalized_id}"
            )
        task_spec = matching_tasks[0]
    try:
        contract_digest = task_contract_digest(contract)
        spec_digest = normalized_task_spec_digest(task_spec)
    except ContractError as exc:
        raise ApprovalWorkflowError(f"invalid executable task for {normalized_id}: {exc}") from exc

    binding = (normalized_id, contract_digest, spec_digest)
    approvals_path = Path(approvals_path)
    with _approval_lock(approvals_path):
        records = _approval_records(approvals_path)
        deduplicated = _deduplicate(records)
        existing = next(
            (record for record in deduplicated if _approval_key(record) == binding),
            None,
        )
        if existing is not None:
            if deduplicated != records:
                _atomic_write(approvals_path, deduplicated)
            return existing, False

        actor = str(approved_by if approved_by is not None else getpass.getuser()).strip()
        record = {
            "task_id": normalized_id,
            "task_contract_sha256": contract_digest,
            "task_spec_sha256": spec_digest,
            "approved_at": approved_at
            or _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
            "approved_by": actor or "unknown",
        }
        deduplicated.append(record)
        _atomic_write(approvals_path, deduplicated)
        return record, True


def consume_approval(
    task_id: str,
    *,
    task_contract_sha256: str,
    task_spec_sha256: str,
    approvals_path: Path = APPROVED_TASKS_PATH,
) -> dict[str, Any] | None:
    """Atomically remove and return one exactly bound approval record."""
    try:
        normalized_id = normalize_task_id(task_id)
    except ContractError:
        return None
    contract_digest = str(task_contract_sha256 or "").strip()
    spec_digest = str(task_spec_sha256 or "").strip()
    if not is_sha256_digest(contract_digest) or not is_sha256_digest(spec_digest):
        return None
    binding = (normalized_id, contract_digest, spec_digest)
    approvals_path = Path(approvals_path)
    with _approval_lock(approvals_path):
        records = _approval_records(approvals_path)
        matches = [record for record in records if _approval_key(record) == binding]
        if matches:
            remaining = [record for record in records if _approval_key(record) != binding]
            _atomic_write(approvals_path, _deduplicate(remaining))
            return matches[0]
    return None


def _validated_restore_record(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise ApprovalWorkflowError("invalid approval record: must be an object")

    required_fields = (
        "task_id",
        "task_contract_sha256",
        "task_spec_sha256",
        "approved_at",
        "approved_by",
    )
    missing = [field_name for field_name in required_fields if field_name not in record]
    if missing:
        raise ApprovalWorkflowError(
            f"invalid approval record: missing {', '.join(missing)}"
        )

    task_id = record["task_id"]
    try:
        normalized_id = normalize_task_id(task_id)
    except ContractError as exc:
        raise ApprovalWorkflowError("invalid approval record: invalid task_id") from exc
    if not isinstance(task_id, str) or task_id != normalized_id:
        raise ApprovalWorkflowError("invalid approval record: task_id must be canonical")

    for field_name in ("task_contract_sha256", "task_spec_sha256"):
        digest = record[field_name]
        if (
            not isinstance(digest, str)
            or digest != digest.strip()
            or not is_sha256_digest(digest)
        ):
            raise ApprovalWorkflowError(
                f"invalid approval record: invalid {field_name}"
            )
    for field_name in ("approved_at", "approved_by"):
        if not isinstance(record[field_name], str) or not record[field_name].strip():
            raise ApprovalWorkflowError(
                f"invalid approval record: invalid {field_name}"
            )

    restored = dict(record)
    try:
        round_tripped = json.loads(
            json.dumps(restored, ensure_ascii=False, allow_nan=False)
        )
    except (TypeError, ValueError) as exc:
        raise ApprovalWorkflowError(
            "invalid approval record: record must be JSON-serializable"
        ) from exc
    if round_tripped != restored:
        raise ApprovalWorkflowError(
            "invalid approval record: record would change when persisted"
        )
    return restored


def restore_approval(
    record: Mapping[str, Any],
    *,
    approvals_path: Path = APPROVED_TASKS_PATH,
) -> bool:
    """Atomically restore one exact digest-bound record after launch rollback."""
    restored = _validated_restore_record(record)
    binding = _approval_key(restored)
    approvals_path = Path(approvals_path)
    with _approval_lock(approvals_path):
        records = _approval_records(approvals_path)
        if any(_approval_key(existing) == binding for existing in records):
            return False
        _atomic_write(approvals_path, [*records, restored])
    return True


def _queue_task_id(task: Mapping[str, Any]) -> str:
    return str(
        task.get("contract_id") or task.get("id") or task.get("session_name") or ""
    ).strip()


def requeue_approved_task(
    task_id: str,
    *,
    queue_path: Path = WORK_QUEUE_PATH,
) -> bool:
    """Move a matching approval-gated queue row back to queued state."""
    try:
        normalized_id = normalize_task_id(task_id)
    except ContractError as exc:
        raise ApprovalWorkflowError(str(exc)) from exc
    with queue_state_lock(Path(queue_path)):
        queue = _load_json_list(Path(queue_path), missing_ok=True)
        changed = False
        for task in queue:
            if not isinstance(task, dict) or _queue_task_id(task) != normalized_id:
                continue
            if task.get("status") != "awaiting_approval":
                continue
            task["status"] = "queued"
            task.pop("blocked_reason", None)
            task.pop("blocked_at", None)
            changed = True
        if changed:
            _atomic_write(Path(queue_path), queue)
        return changed


# Queue statuses for which a contract-gated task still needs a human approval
# decision. "" covers a contract whose task has no queue row yet.
_APPROVAL_ACTIONABLE_STATUSES = frozenset({"", "queued", "blocked", "proposed"})


def list_pending_approvals(
    *,
    queue_path: Path = WORK_QUEUE_PATH,
    contracts_path: Path = TASK_CONTRACTS_PATH,
    approvals_path: Path = APPROVED_TASKS_PATH,
) -> list[dict[str, Any]]:
    """Return queue rows and approval-gated contracts still needing a decision."""
    pending: dict[str, dict[str, Any]] = {}
    contracts = load_contracts(Path(contracts_path))
    queue_status: dict[str, str] = {}

    for index, task in enumerate(_load_json_list(Path(queue_path), missing_ok=True)):
        if not isinstance(task, Mapping):
            continue
        task_id = _queue_task_id(task) or f"queue-entry-{index + 1}"
        queue_status.setdefault(task_id, str(task.get("status") or "").strip())
        if task.get("status") != "awaiting_approval":
            continue
        contract = contracts.get(task_id)
        approval_matches = False
        if contract is not None:
            try:
                approval_matches = approval_logged(
                    task_id,
                    Path(approvals_path),
                    task_contract_sha256=task_contract_digest(contract),
                    task_spec_sha256=normalized_task_spec_digest(task),
                )
            except ContractError:
                approval_matches = False
        pending[task_id] = {
            "task_id": task_id,
            "status": "awaiting_approval",
            "description": str(
                task.get("task") or task.get("title") or task.get("description") or ""
            ).strip(),
            "approval_logged": approval_matches,
            "sources": ["work_queue"],
        }

    for task_id, contract in sorted(contracts.items()):
        if not contract.requires_approval:
            continue
        if task_id in pending:
            pending[task_id]["sources"].append("contract")
            if not pending[task_id]["description"]:
                pending[task_id]["description"] = contract.description
            continue
        if queue_status.get(task_id, "") not in _APPROVAL_ACTIONABLE_STATUSES:
            # The task already moved past the approval gate (dispatched, completed,
            # verified, or awaiting Codex review), so no human decision is pending.
            continue
        pending[task_id] = {
            "task_id": task_id,
            "status": "requires_approval",
            "description": contract.description,
            "approval_logged": False,
            "sources": ["contract"],
        }

    return [pending[task_id] for task_id in sorted(pending)]
