"""Typed contracts and durable checkpoints for orchestration loops.

Two contract layers live here:

1. TaskSpec / AttemptRecord — the dispatch-time execution contract used by
   orchestrator_loop.py and harness/runtime_launcher.py (scope, budget,
   verification evidence).

2. TaskContract — the machine-readable safety contract (CODEX-8).  Every task
   in WORK_QUEUE.json must have a TaskContract in TASK_CONTRACTS.json before
   the orchestrator will execute it without human approval.  It declares what
   a task needs (inputs, capabilities), what it produces (outputs), what side
   effects it has, and what safety checks must pass before autonomous
   execution.
"""

from __future__ import annotations

import datetime as _datetime
import fnmatch
import hashlib
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional


class ContractError(ValueError):
    """Raised when a queue task cannot become an executable contract."""


def _now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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

    def for_dispatch(self) -> "TaskSpec":
        """Return the normalized executable form after typed authorization."""
        if not self.legacy_adapter:
            return self
        return TaskSpec.from_queue_task({**self.to_dict(), "legacy_adapter": False})

    @property
    def task_spec_hash(self) -> str:
        return _canonical_sha256(self.to_dict())

    @property
    def contract_hash(self) -> str:
        """Backward-compatible name for the dispatch TaskSpec digest."""
        return self.task_spec_hash


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


# ══════════════════════════════════════════════════════════════════════════════
# Typed Task Contract (CODEX-8) — machine-readable specification for
# autonomous task execution.  Every task in WORK_QUEUE.json must have a
# contract before the orchestrator will execute it without human approval.
# ══════════════════════════════════════════════════════════════════════════════

_log = logging.getLogger(__name__)

_HARNESS_REPO_ROOT = Path(__file__).resolve().parent.parent
TASK_CONTRACTS_PATH = _HARNESS_REPO_ROOT / "TASK_CONTRACTS.json"
APPROVED_TASKS_PATH = _HARNESS_REPO_ROOT / "approved_tasks.json"


class TaskType(str, Enum):
    CODE = "code"           # writes/modifies code
    RESEARCH = "research"   # reads/analyzes, no mutations
    FILE_OP = "file_op"     # creates/modifies files
    API_CALL = "api_call"   # calls external APIs
    ANALYSIS = "analysis"   # data analysis, no mutations
    VOICE = "voice"         # voice/audio related
    SYSTEM = "system"       # system config, launchd, etc.
    PLANNING = "planning"   # produces plans, no execution


class SideEffect(str, Enum):
    WRITES_FILES = "writes_files"
    MODIFIES_GIT = "modifies_git"
    NETWORK_REQUESTS = "network_requests"
    SUBPROCESS = "subprocess"
    MODIFIES_CONFIG = "modifies_config"
    SENDS_MESSAGES = "sends_messages"
    MODIFIES_STATE = "modifies_state"   # in-memory state changes


class Capability(str, Enum):
    OLLAMA = "ollama"           # local LLM
    FILESYSTEM = "filesystem"   # read/write files
    INTERNET = "internet"       # outbound HTTP
    GIT = "git"                 # git operations
    PYTHON = "python"           # python subprocess
    VOICE = "voice"             # microphone/speaker
    CALENDAR = "calendar"       # calendar access
    IMESSAGE = "imessage"       # messaging
    SCREEN = "screen"           # screen capture/control


@dataclass
class InputSpec:
    name: str
    type: str               # "str", "int", "file_path", "json", etc.
    required: bool = True
    description: str = ""
    default: Optional[str] = None


@dataclass
class OutputSpec:
    name: str
    type: str               # "file", "json", "str", "commit_hash", etc.
    path_template: str = ""  # e.g. "logs/{task_id}_result.json"
    description: str = ""


@dataclass
class TaskContract:
    # Identity
    task_id: str                    # matches contract_id/session_name in WORK_QUEUE
    task_type: TaskType
    description: str
    contract_version: str = "1.0"

    # I/O
    inputs: list[InputSpec] = field(default_factory=list)
    outputs: list[OutputSpec] = field(default_factory=list)

    # Safety
    side_effects: list[SideEffect] = field(default_factory=list)
    requires_capabilities: list[Capability] = field(default_factory=list)
    reversible: bool = True
    requires_approval: bool = False  # True = must have human sign-off

    # Execution
    entry_point: str = ""           # script or function to call
    working_directory: str = "/Users/truthseeker/jarvis-ai"
    estimated_tokens: int = 2000
    max_duration_seconds: int = 300

    # Validation
    preconditions: list[str] = field(default_factory=list)   # must be true before run
    postconditions: list[str] = field(default_factory=list)  # verified after run

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["task_type"] = str(
            self.task_type.value if isinstance(self.task_type, TaskType) else self.task_type
        )
        data["side_effects"] = [
            str(e.value if isinstance(e, SideEffect) else e) for e in self.side_effects
        ]
        data["requires_capabilities"] = [
            str(c.value if isinstance(c, Capability) else c)
            for c in self.requires_capabilities
        ]
        return data

    @property
    def contract_hash(self) -> str:
        return _canonical_sha256(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskContract":
        if not isinstance(data, Mapping):
            raise ContractError("contract entry must be an object")
        try:
            task_type = TaskType(str(data.get("task_type", "")))
        except ValueError as exc:
            raise ContractError(f"unknown task_type: {data.get('task_type')!r}") from exc
        try:
            side_effects = [SideEffect(str(e)) for e in data.get("side_effects", [])]
        except ValueError as exc:
            raise ContractError(f"unknown side_effect in {data.get('side_effects')!r}") from exc
        try:
            capabilities = [Capability(str(c)) for c in data.get("requires_capabilities", [])]
        except ValueError as exc:
            raise ContractError(
                f"unknown capability in {data.get('requires_capabilities')!r}"
            ) from exc
        return cls(
            task_id=str(data.get("task_id", "")),
            task_type=task_type,
            description=str(data.get("description", "")),
            contract_version=str(data.get("contract_version", "1.0")),
            inputs=[InputSpec(**dict(i)) for i in data.get("inputs", [])],
            outputs=[OutputSpec(**dict(o)) for o in data.get("outputs", [])],
            side_effects=side_effects,
            requires_capabilities=capabilities,
            reversible=bool(data.get("reversible", True)),
            requires_approval=bool(data.get("requires_approval", False)),
            entry_point=str(data.get("entry_point", "")),
            working_directory=str(
                data.get("working_directory", "/Users/truthseeker/jarvis-ai")
            ),
            estimated_tokens=int(data.get("estimated_tokens", 2000)),
            max_duration_seconds=int(data.get("max_duration_seconds", 300)),
            preconditions=[str(p) for p in data.get("preconditions", [])],
            postconditions=[str(p) for p in data.get("postconditions", [])],
        )


def validate_contract(contract: TaskContract) -> tuple[bool, list[str]]:
    """Validate a TaskContract. Returns (is_valid, list_of_errors)."""
    errors: list[str] = []
    if not str(contract.task_id or "").strip():
        errors.append("task_id must not be empty")
    try:
        TaskType(contract.task_type)
    except ValueError:
        errors.append(f"task_type is not a valid TaskType: {contract.task_type!r}")
    try:
        effects = {SideEffect(e) for e in contract.side_effects}
    except ValueError as exc:
        effects = set()
        errors.append(f"side_effects contains an unknown value: {exc}")
    if SideEffect.WRITES_FILES in effects and not contract.outputs:
        errors.append("side_effects includes writes_files but outputs is empty")
    if contract.requires_approval and not contract.preconditions:
        errors.append("requires_approval=True but preconditions is empty")
    return (not errors, errors)


def load_contracts(path: Path = TASK_CONTRACTS_PATH) -> dict[str, TaskContract]:
    """Load TASK_CONTRACTS.json. Returns dict keyed by task_id.

    Corrupt or unparseable entries are skipped with a warning so one bad
    contract never blocks the rest of the fleet.
    """
    path = Path(path)
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("[TaskContract] Could not read %s: %s", path, exc)
        return {}
    if not isinstance(data, list):
        _log.warning("[TaskContract] %s is not a JSON list — ignoring", path)
        return {}
    contracts: dict[str, TaskContract] = {}
    for entry in data:
        try:
            contract = TaskContract.from_dict(entry)
        except (ContractError, TypeError, ValueError) as exc:
            _log.warning("[TaskContract] Skipping invalid contract entry: %s", exc)
            continue
        if contract.task_id:
            contracts[contract.task_id] = contract
    return contracts


def save_contracts(
    contracts: dict[str, TaskContract], path: Path = TASK_CONTRACTS_PATH
) -> None:
    """Atomically save contracts to TASK_CONTRACTS.json (sorted by task_id)."""
    path = Path(path)
    payload = [contracts[key].to_dict() for key in sorted(contracts)]
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def contract_for_task(
    task_id: str, path: Path = TASK_CONTRACTS_PATH
) -> Optional[TaskContract]:
    """Convenience lookup: return the TaskContract for task_id, or None."""
    if not task_id:
        return None
    return load_contracts(path).get(task_id)


def approval_logged(
    task_id: str,
    path: Path = APPROVED_TASKS_PATH,
    *,
    task_contract_sha256: str = "",
    task_spec_sha256: str = "",
) -> bool:
    """True if an approval is bound to the exact contract and executable spec.

    The CODEX-11 identity/audit fields remain required. Older records without
    both digest fields intentionally fail closed.
    """
    normalized_id = str(task_id or "").strip()
    contract_digest = str(task_contract_sha256 or "").strip()
    spec_digest = str(task_spec_sha256 or "").strip()
    if not normalized_id or not contract_digest or not spec_digest:
        return False
    path = Path(path)
    if not path.exists():
        return False
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("[TaskContract] Could not read %s: %s", path, exc)
        return False
    if not isinstance(data, list):
        return False
    return any(
        isinstance(entry, Mapping)
        and entry.get("task_id") == normalized_id
        and entry.get("task_contract_sha256") == contract_digest
        and entry.get("task_spec_sha256") == spec_digest
        for entry in data
    )
