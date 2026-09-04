"""Bounded observe-plan-act-verify loop for Jarvis V2."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .model import LocalModelError, ModelClient, ModelTurn
from .tools import LocalToolError, model_tool_schemas


SYSTEM_PROMPT = """You are Jarvis V2, a local code-inspection agent.
Use tools when evidence is needed. Never claim a tool ran unless its result appears
in this conversation. When the task is complete, answer directly with evidence.
All inference and tool execution occurs on the user's Mac."""

_RUN_ID_RE = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class AgentLimits:
    max_steps: int = 8
    max_seconds: float = 300.0
    max_consecutive_errors: int = 2
    max_repeated_call: int = 2
    max_total_tokens: int = 32_000

    def __post_init__(self) -> None:
        if min(
            self.max_steps,
            self.max_seconds,
            self.max_consecutive_errors,
            self.max_repeated_call,
            self.max_total_tokens,
        ) <= 0:
            raise ValueError("all agent limits must be positive")


@dataclass
class AgentState:
    run_id: str
    task: str
    status: str = "running"
    step: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    consecutive_errors: int = 0
    last_call_digest: str = ""
    repeated_call_count: int = 0
    tool_calls_completed: int = 0
    tool_evidence: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    final_answer: str = ""
    reason: str = ""


@dataclass(frozen=True)
class AgentResult:
    run_id: str
    status: str
    answer: str
    steps: int
    reason: str
    checkpoint_path: Path
    event_log_path: Path
    prompt_tokens: int
    completion_tokens: int
    tool_evidence: tuple["ToolEvidence", ...]


@dataclass(frozen=True)
class ToolEvidence:
    call_id: str
    tool: str
    arguments_sha256: str
    result_sha256: str
    result_chars: int
    step: int


class LocalAgentLoop:
    """Run one local task and checkpoint every observable transition."""

    def __init__(
        self,
        *,
        model: ModelClient,
        execute_tool: Callable[[str, dict[str, Any]], str],
        state_dir: Path,
        limits: AgentLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
        is_cancelled: Callable[[], bool] = lambda: False,
        require_tool_evidence: bool = False,
        allow_tools: bool = True,
    ) -> None:
        self.model = model
        self.execute_tool = execute_tool
        self.state_dir = state_dir.expanduser().resolve(strict=False)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir.chmod(0o700)
        self.limits = limits or AgentLimits()
        self.clock = clock
        self.is_cancelled = is_cancelled
        self.require_tool_evidence = require_tool_evidence
        self.allow_tools = allow_tools
        self._checkpoint_lock = threading.Lock()

    def _path(self, run_id: str) -> Path:
        self._validate_run_id(run_id)
        return self.state_dir / f"{run_id}.json"

    def _event_path(self, run_id: str) -> Path:
        self._validate_run_id(run_id)
        return self.state_dir / f"{run_id}.events.jsonl"

    def _lease_path(self, run_id: str) -> Path:
        self._validate_run_id(run_id)
        return self.state_dir / f"{run_id}.lock"

    def _acquire_run_lease(self, run_id: str):
        """Own a run checkpoint exclusively across threads and processes."""
        path = self._lease_path(run_id)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(path, 0o600)
        handle = os.fdopen(descriptor, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError(f"run {run_id} is already owned by another worker") from exc
        return handle

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
            raise ValueError("run id must be exactly 32 lowercase hexadecimal characters")

    def _checkpoint(self, state: AgentState) -> Path:
        with self._checkpoint_lock:
            path = self._path(state.run_id)
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(state), indent=2, sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            temporary.replace(path)
            event_path = self._event_path(state.run_id)
            sequence = 1
            if event_path.exists():
                with event_path.open("r", encoding="utf-8") as handle:
                    sequence += sum(1 for _ in handle)
            event = {
                "sequence": sequence,
                "run_id": state.run_id,
                "status": state.status,
                "step": state.step,
                "reason": state.reason,
                "checkpoint_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            with event_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            event_path.chmod(0o600)
            return path

    def _new_state(self, task: str) -> AgentState:
        run_id = uuid.uuid4().hex
        return AgentState(
            run_id=run_id,
            task=task,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": task},
            ],
        )

    def load(self, run_id: str) -> AgentState:
        self._validate_run_id(run_id)
        payload = json.loads(self._path(run_id).read_text(encoding="utf-8"))
        state = AgentState(**payload)
        if state.run_id != run_id:
            raise ValueError("checkpoint run id does not match its filename")
        if state.status not in {"running", "blocked", "cancelled", "completed"}:
            raise ValueError("checkpoint has an invalid status")
        if not isinstance(state.messages, list) or not all(
            isinstance(message, dict) for message in state.messages
        ):
            raise ValueError("checkpoint messages must be a list of objects")
        return state

    @staticmethod
    def _parse_tool_call(turn: ModelTurn) -> tuple[str, str, dict[str, Any]]:
        if len(turn.tool_calls) != 1:
            raise LocalToolError("model must request exactly one tool per step")
        call = turn.tool_calls[0]
        try:
            call_id = str(call["id"])
            function = call["function"]
            name = str(function["name"])
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise LocalToolError("model returned a malformed tool call") from exc
        if not call_id or not name or not isinstance(arguments, dict):
            raise LocalToolError("model returned a malformed tool call")
        return call_id, name, arguments

    def run(self, task: str | None = None, *, resume_run_id: str | None = None) -> AgentResult:
        if resume_run_id:
            state = self.load(resume_run_id)
            if state.status not in {"running", "blocked", "cancelled"}:
                return self._result(state)
            state.status = "running"
            state.reason = ""
        else:
            cleaned = (task or "").strip()
            if not cleaned:
                raise ValueError("task must be non-empty")
            state = self._new_state(cleaned)
        lease = self._acquire_run_lease(state.run_id)
        try:
            return self._run_owned(state)
        finally:
            fcntl.flock(lease.fileno(), fcntl.LOCK_UN)
            lease.close()

    def _run_owned(self, state: AgentState) -> AgentResult:
        self._checkpoint(state)
        started = self.clock()

        while state.step < self.limits.max_steps:
            if self.is_cancelled():
                state.status = "cancelled"
                state.reason = "cancelled by owner"
                break
            if self.clock() - started >= self.limits.max_seconds:
                state.status = "blocked"
                state.reason = "time budget exhausted"
                break
            state.step += 1
            try:
                schemas = model_tool_schemas() if self.allow_tools else []
                turn = self.model.complete(state.messages, schemas)
                state.prompt_tokens += turn.prompt_tokens
                state.completion_tokens += turn.completion_tokens
                if state.prompt_tokens + state.completion_tokens > self.limits.max_total_tokens:
                    raise LocalModelError("cumulative token budget exhausted")
                if turn.finish_reason == "length":
                    raise LocalModelError("local model output was truncated at its token limit")
                if turn.tool_calls and not self.allow_tools:
                    raise LocalModelError("model requested a tool in a no-tools phase")
                if not turn.tool_calls:
                    answer = turn.content.strip()
                    if not answer:
                        raise LocalModelError("local model returned no answer or tool call")
                    if self.require_tool_evidence and state.tool_calls_completed == 0:
                        raise LocalModelError(
                            "task requires tool evidence before a final answer"
                        )
                    state.final_answer = answer
                    state.messages.append({"role": "assistant", "content": answer})
                    state.status = "completed"
                    state.reason = "model returned a final answer"
                    self._checkpoint(state)
                    return self._result(state)

                call_id, name, arguments = self._parse_tool_call(turn)
                digest = hashlib.sha256(
                    json.dumps(
                        {"name": name, "arguments": arguments},
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                if digest == state.last_call_digest:
                    state.repeated_call_count += 1
                else:
                    state.last_call_digest = digest
                    state.repeated_call_count = 1
                if state.repeated_call_count > self.limits.max_repeated_call:
                    raise LocalToolError("model repeated the same tool call without progress")

                result = self.execute_tool(name, arguments)
                evidence = ToolEvidence(
                    call_id=call_id,
                    tool=name,
                    arguments_sha256=hashlib.sha256(
                        json.dumps(
                            arguments,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    result_sha256=hashlib.sha256(result.encode("utf-8")).hexdigest(),
                    result_chars=len(result),
                    step=state.step,
                )
                state.messages.append(
                    {
                        "role": "assistant",
                        "content": turn.content,
                        "tool_calls": list(turn.tool_calls),
                    }
                )
                state.messages.append(
                    {"role": "tool", "tool_call_id": call_id, "content": result}
                )
                state.tool_calls_completed += 1
                state.tool_evidence.append(asdict(evidence))
                state.consecutive_errors = 0
            except Exception as exc:
                state.consecutive_errors += 1
                state.reason = f"{type(exc).__name__}: {exc}"
                state.messages.append(
                    {"role": "user", "content": f"Previous step failed validation: {exc}"}
                )
                if state.consecutive_errors >= self.limits.max_consecutive_errors:
                    state.status = "blocked"
                    break
            self._checkpoint(state)

        if state.status == "running":
            state.status = "blocked"
            state.reason = "step budget exhausted"
        self._checkpoint(state)
        return self._result(state)

    def _result(self, state: AgentState) -> AgentResult:
        return AgentResult(
            run_id=state.run_id,
            status=state.status,
            answer=state.final_answer,
            steps=state.step,
            reason=state.reason,
            checkpoint_path=self._path(state.run_id),
            event_log_path=self._event_path(state.run_id),
            prompt_tokens=state.prompt_tokens,
            completion_tokens=state.completion_tokens,
            tool_evidence=tuple(ToolEvidence(**item) for item in state.tool_evidence),
        )
