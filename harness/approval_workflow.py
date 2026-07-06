"""Human approval records for contract-gated orchestration tasks."""

from __future__ import annotations

import datetime as _datetime
import getpass
import json
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from harness.task_contract import (
    APPROVED_TASKS_PATH,
    TASK_CONTRACTS_PATH,
    contract_for_task,
    load_contracts,
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
        record["task_id"] = str(record["task_id"]).strip()
        normalized.append(record)
    return normalized


def _deduplicate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduplicated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        task_id = str(record["task_id"]).strip()
        if task_id in seen:
            continue
        seen.add(task_id)
        deduplicated.append(record)
    return deduplicated


def _atomic_write(path: Path, records: list[dict[str, Any]]) -> None:
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as handle:
            json.dump(records, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
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
    contracts_path: Path = TASK_CONTRACTS_PATH,
    approvals_path: Path = APPROVED_TASKS_PATH,
) -> tuple[dict[str, Any], bool]:
    """Record one contract approval atomically; return ``(record, created)``."""
    normalized_id = str(task_id or "").strip()
    if not normalized_id or contract_for_task(normalized_id, Path(contracts_path)) is None:
        raise ApprovalWorkflowError(f"no task contract found for: {normalized_id or task_id}")

    records = _approval_records(Path(approvals_path))
    deduplicated = _deduplicate(records)
    existing = next(
        (record for record in deduplicated if record["task_id"] == normalized_id),
        None,
    )
    if existing is not None:
        if deduplicated != records:
            _atomic_write(Path(approvals_path), deduplicated)
        return existing, False

    actor = str(approved_by if approved_by is not None else getpass.getuser()).strip()
    record = {
        "task_id": normalized_id,
        "approved_at": approved_at
        or _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
        "approved_by": actor or "unknown",
    }
    deduplicated.append(record)
    _atomic_write(Path(approvals_path), deduplicated)
    return record, True


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
    normalized_id = str(task_id or "").strip()
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


def list_pending_approvals(
    *,
    queue_path: Path = WORK_QUEUE_PATH,
    contracts_path: Path = TASK_CONTRACTS_PATH,
    approvals_path: Path = APPROVED_TASKS_PATH,
) -> list[dict[str, Any]]:
    """Return awaiting queue rows plus unapproved approval-gated contracts."""
    approved_ids = {
        str(record["task_id"]).strip()
        for record in _deduplicate(_approval_records(Path(approvals_path)))
    }
    pending: dict[str, dict[str, Any]] = {}

    for index, task in enumerate(_load_json_list(Path(queue_path), missing_ok=True)):
        if not isinstance(task, Mapping) or task.get("status") != "awaiting_approval":
            continue
        task_id = _queue_task_id(task) or f"queue-entry-{index + 1}"
        pending[task_id] = {
            "task_id": task_id,
            "status": "awaiting_approval",
            "description": str(
                task.get("task") or task.get("title") or task.get("description") or ""
            ).strip(),
            "approval_logged": task_id in approved_ids,
            "sources": ["work_queue"],
        }

    for task_id, contract in sorted(load_contracts(Path(contracts_path)).items()):
        if not contract.requires_approval or task_id in approved_ids:
            continue
        if task_id in pending:
            pending[task_id]["sources"].append("contract")
            if not pending[task_id]["description"]:
                pending[task_id]["description"] = contract.description
            continue
        pending[task_id] = {
            "task_id": task_id,
            "status": "requires_approval",
            "description": contract.description,
            "approval_logged": False,
            "sources": ["contract"],
        }

    return [pending[task_id] for task_id in sorted(pending)]
