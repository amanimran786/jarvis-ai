"""Typed contracts and durable checkpoints for orchestration loops."""

from __future__ import annotations

import datetime as _datetime
import fnmatch
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


class ContractError(ValueError):
    """Raised when a queue task cannot become an executable contract."""


def _now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


def _strings(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        raise ContractError(f"{field_name} must be a string or list of strings")
    result = tuple(str(item).strip() for item in values if str(item).strip())
    return tuple(dict.fromkeys(result))


def _relative_paths(value: Any, field_name: str) -> tuple[str, ...]:
    paths = _strings(value, field_name)
    for raw in paths:
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts or "\x00" in raw:
            raise ContractError(f"{field_name} contains unsafe path: {raw}")
    return paths


def _positive_int(value: Any, default: int, field_name: str) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{field_name} must be an integer") from exc
    if parsed <= 0:
        raise ContractError(f"{field_name} must be greater than zero")
    return parsed


@dataclass(frozen=True)
class TaskBudget:
    max_attempts: int = 3
    wall_time_seconds: int = 1800
    tool_calls: int = 40

    @classmethod
    def from_value(cls, value: Any) -> "TaskBudget":
        data = value if isinstance(value, Mapping) else {}
        return cls(
            max_attempts=_positive_int(data.get("max_attempts"), 3, "budget.max_attempts"),
            wall_time_seconds=_positive_int(
                data.get("wall_time_seconds"), 1800, "budget.wall_time_seconds"
            ),
            tool_calls=_positive_int(data.get("tool_calls"), 40, "budget.tool_calls"),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "max_attempts": self.max_attempts,
            "wall_time_seconds": self.wall_time_seconds,
            "tool_calls": self.tool_calls,
        }


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    title: str
    goal: str
    description: str
    allowed_files: tuple[str, ...] = ()
    forbidden_files: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    verification_commands: tuple[str, ...] = ()
    constraints: Mapping[str, Any] = field(default_factory=dict)
    budget: TaskBudget = field(default_factory=TaskBudget)
    domain: str = "general"
    assigned_ai: str = "claude"
    legacy_adapter: bool = False

    @classmethod
    def from_queue_task(cls, task: Mapping[str, Any]) -> "TaskSpec":
        if not isinstance(task, Mapping):
            raise ContractError("task must be an object")

        legacy_text = str(task.get("task") or "").strip()
        title = str(task.get("title") or legacy_text).strip()
        description = str(task.get("description") or task.get("notes") or legacy_text).strip()
        goal = str(task.get("goal") or description or title).strip()
        if not title or not goal:
            raise ContractError("task requires a title/task and a goal/description")

        explicit_id = str(task.get("id") or "").strip()
        legacy_adapter = bool(task.get("legacy_adapter", not bool(explicit_id)))
        if explicit_id:
            task_id = explicit_id
        else:
            digest = hashlib.sha256(f"{title}\n{description}".encode("utf-8")).hexdigest()[:12]
            task_id = f"LEGACY-{digest}"

        allowed = task.get("allowed_files", task.get("files_hint", ()))
        constraints = task.get("constraints")
        if constraints is None:
            constraints = {"local_first": True}
        if not isinstance(constraints, Mapping):
            raise ContractError("constraints must be an object")
        try:
            json.dumps(dict(constraints), sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ContractError("constraints must contain JSON-serializable values") from exc

        return cls(
            task_id=task_id,
            title=title,
            goal=goal,
            description=description,
            allowed_files=_relative_paths(allowed, "allowed_files"),
            forbidden_files=_relative_paths(task.get("forbidden_files"), "forbidden_files"),
            acceptance_criteria=_strings(task.get("acceptance_criteria"), "acceptance_criteria"),
            verification_commands=_strings(
                task.get("verification_commands", task.get("tests")),
                "verification_commands",
            ),
            constraints=dict(constraints),
            budget=TaskBudget.from_value(task.get("budget")),
            domain=str(task.get("domain") or "general").strip() or "general",
            assigned_ai=str(task.get("assigned_ai") or "claude").strip() or "claude",
            legacy_adapter=legacy_adapter,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.task_id,
            "title": self.title,
            "goal": self.goal,
            "description": self.description,
            "allowed_files": list(self.allowed_files),
            "forbidden_files": list(self.forbidden_files),
            "acceptance_criteria": list(self.acceptance_criteria),
            "verification_commands": list(self.verification_commands),
            "constraints": dict(self.constraints),
            "budget": self.budget.to_dict(),
            "domain": self.domain,
            "assigned_ai": self.assigned_ai,
            "legacy_adapter": self.legacy_adapter,
        }

    @property
    def contract_hash(self) -> str:
        encoded = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CompletionVerdict:
    status: str
    failure_class: str = ""
    reasons: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status == "verified"


def _path_matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(
        path == pattern
        or path.startswith(pattern.rstrip("/") + "/")
        or fnmatch.fnmatch(path, pattern)
        for pattern in patterns
    )


def evaluate_completion(
    spec: TaskSpec,
    evidence: Mapping[str, Any] | None,
) -> CompletionVerdict:
    """Judge loop-observed evidence without trusting the agent's summary."""
    if not isinstance(evidence, Mapping) or evidence.get("observer") != "loop":
        return CompletionVerdict(
            "unverified",
            "verification_missing",
            ("completion evidence was not produced by the loop",),
        )

    try:
        changed_files = _relative_paths(evidence.get("changed_files"), "changed_files")
    except ContractError as exc:
        return CompletionVerdict("rejected", "scope_violation", (str(exc),))

    forbidden = [path for path in changed_files if _path_matches(path, spec.forbidden_files)]
    if forbidden:
        return CompletionVerdict(
            "rejected",
            "scope_violation",
            (f"forbidden files changed: {', '.join(forbidden)}",),
        )

    outside_scope = [
        path
        for path in changed_files
        if spec.allowed_files and not _path_matches(path, spec.allowed_files)
    ]
    if outside_scope:
        return CompletionVerdict(
            "rejected",
            "scope_violation",
            (f"files changed outside contract: {', '.join(outside_scope)}",),
        )
    if spec.allowed_files and not changed_files:
        return CompletionVerdict(
            "unverified",
            "verification_missing",
            ("contract declared editable files but no changed-file evidence was captured",),
        )

    command_results = evidence.get("commands", [])
    if not isinstance(command_results, list):
        return CompletionVerdict(
            "unverified",
            "verification_missing",
            ("command evidence must be a list",),
        )
    observed_commands = {
        str(item.get("command", "")): item
        for item in command_results
        if isinstance(item, Mapping) and item.get("command")
    }
    missing_commands = [
        command for command in spec.verification_commands if command not in observed_commands
    ]
    if missing_commands:
        return CompletionVerdict(
            "unverified",
            "verification_missing",
            (f"verification commands not observed: {', '.join(missing_commands)}",),
        )
    failed_commands = [
        command
        for command in spec.verification_commands
        if observed_commands[command].get("exit_code") != 0
    ]
    if failed_commands:
        return CompletionVerdict(
            "rejected",
            "test_failure",
            (f"verification commands failed: {', '.join(failed_commands)}",),
        )

    findings = evidence.get("policy_findings", [])
    if not isinstance(findings, list):
        return CompletionVerdict(
            "unverified",
            "verification_missing",
            ("policy findings evidence must be a list",),
        )
    unresolved = [
        finding
        for finding in findings
        if not isinstance(finding, Mapping)
        or str(finding.get("status", "open")).lower() not in {"resolved", "waived"}
    ]
    if unresolved:
        return CompletionVerdict(
            "rejected",
            "policy_failure",
            (f"{len(unresolved)} unresolved policy finding(s)",),
        )

    if not changed_files and not command_results and not evidence.get("artifact_reviewed"):
        return CompletionVerdict(
            "unverified",
            "verification_missing",
            ("no observable completion evidence was captured",),
        )
    return CompletionVerdict("verified")


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    task_id: str
    session_id: str
    phase: str
    status: str
    contract_sha256: str
    created_at: str
    attempt_number: int
    agent: str
    remaining_budget: Mapping[str, int]
    evidence: Mapping[str, Any] = field(default_factory=dict)
    failure_class: str = ""

    @classmethod
    def dispatched(
        cls,
        spec: TaskSpec,
        session_id: str,
        *,
        attempt_number: int = 1,
    ) -> "AttemptRecord":
        return cls(
            attempt_id="attempt_" + uuid.uuid4().hex[:16],
            task_id=spec.task_id,
            session_id=session_id,
            phase="dispatch",
            status="pending",
            contract_sha256=spec.contract_hash,
            created_at=_now(),
            attempt_number=attempt_number,
            agent=spec.assigned_ai,
            remaining_budget=spec.budget.to_dict(),
        )

    @classmethod
    def checkpoint(
        cls,
        spec: TaskSpec,
        session_id: str,
        attempt_id: str,
        *,
        phase: str,
        status: str,
        evidence: Mapping[str, Any] | None = None,
        failure_class: str = "",
        attempt_number: int = 1,
    ) -> "AttemptRecord":
        return cls(
            attempt_id=attempt_id,
            task_id=spec.task_id,
            session_id=session_id,
            phase=phase,
            status=status,
            contract_sha256=spec.contract_hash,
            created_at=_now(),
            attempt_number=attempt_number,
            agent=spec.assigned_ai,
            remaining_budget=spec.budget.to_dict(),
            evidence=dict(evidence or {}),
            failure_class=failure_class,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "phase": self.phase,
            "status": self.status,
            "contract_sha256": self.contract_sha256,
            "created_at": self.created_at,
            "attempt_number": self.attempt_number,
            "agent": self.agent,
            "remaining_budget": dict(self.remaining_budget),
            "evidence": dict(self.evidence),
            "failure_class": self.failure_class,
        }


class AttemptStore:
    """Append-only JSONL checkpoint store for loop phase transitions."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: AttemptRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    records.append(json.loads(line))
        return records
