#!/usr/bin/env python3
"""Emit sub-step pipeline traces for Jarvis V2 runs without modifying `jarvis_v2`.

`LocalAgentLoop` and `LocalAgentTeam` both take their model client and tool plane
by injection. This module decorates those two seams, so a traced run is byte-for-byte
the same run with an extra observer attached. Nothing here mutates agent state,
acquires a run lease, or writes into a run's checkpoint directory.

Why this exists: a checkpoint only lands once per step, so the file on disk is
silent for the entire duration of a model request. On this machine a single step
has been measured at 12.6 s wall clock with 11.3 s of that spent before the first
token arrived. A checkpoint-only dashboard shows nothing for those 11.3 s. These
trace records close that gap.

Traces are written to `.jarvis-v2/traces/<trace_id>.jsonl`, which is gitignored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis_v2.agent import AgentLimits, LocalAgentLoop
from jarvis_v2.config import LocalConfigurationError, LocalModelConfig
from jarvis_v2.model import LocalMLXClient
from jarvis_v2.team import (
    AcceptanceContract,
    AgentAssignment,
    LocalAgentTeam,
)
from jarvis_v2.tools import ReadOnlyLocalTools

TRACE_ID_RE = re.compile(r"[0-9a-f]{32}")
DEFAULT_TRACE_DIR = Path(".jarvis-v2/traces")

# Checkpoints contain the full local conversation, including tool results. Trace
# content is separately redacted by default and only persisted with an explicit
# opt-in intended for foreground debugging.
RESULT_PREVIEW_CHARS = 400
CONTENT_PREVIEW_CHARS = 400


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return _text_digest(encoded)


def _sensitive_text_fields(
    writer: "TraceWriter",
    name: str,
    value: str,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        f"{name}_chars": len(value),
        f"{name}_sha256": _text_digest(value),
    }
    if writer.include_sensitive_content:
        fields[name] = value
    return fields


def validate_trace_id(trace_id: str) -> None:
    """Reject anything that could escape the trace directory."""
    if not isinstance(trace_id, str) or TRACE_ID_RE.fullmatch(trace_id) is None:
        raise ValueError("trace id must be exactly 32 lowercase hexadecimal characters")


class TraceWriter:
    """Append-only, thread-safe trace sink shared by every worker in a run."""

    def __init__(
        self,
        trace_dir: Path,
        trace_id: str | None = None,
        *,
        include_sensitive_content: bool = False,
    ) -> None:
        self.trace_id = trace_id or uuid.uuid4().hex
        validate_trace_id(self.trace_id)
        self.trace_dir = trace_dir.expanduser().resolve(strict=False)
        self.trace_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.trace_dir / f"{self.trace_id}.jsonl"
        self._lock = threading.Lock()
        self._sequence = 0
        self.include_sensitive_content = include_sensitive_content
        # Wall clock is recorded once, alongside the monotonic origin, so the
        # dashboard can place a monotonic trace on a human timeline. Every other
        # timestamp stays monotonic, matching `ModelTimingEvidence`.
        self.emit(
            "trace_started",
            trace_id=self.trace_id,
            wall_clock_epoch=time.time(),
            monotonic_origin=time.monotonic(),
        )

    def emit(self, kind: str, **fields: Any) -> None:
        with self._lock:
            self._sequence += 1
            record = {
                "sequence": self._sequence,
                "t": time.monotonic(),
                "kind": kind,
                **fields,
            }
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
            self.path.chmod(0o600)


class TracingModelClient:
    """Wrap a `ModelClient`, bracketing every request with trace records.

    Satisfies the same protocol as `LocalMLXClient`, so `LocalAgentLoop` cannot
    tell the difference. The wrapped client's own `ModelTurn` timings are copied
    into the finish record so the trace and the checkpoint agree by construction.
    """

    def __init__(self, inner: Any, writer: TraceWriter, *, actor: str) -> None:
        self._inner = inner
        self._writer = writer
        self._actor = actor
        self._count = 0

    def ready(self) -> bool:
        return self._inner.ready()

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        return self._complete_traced(
            messages,
            tools,
            lambda: self._inner.complete(messages, tools),
        )

    def complete_cancellable(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        is_cancelled: Callable[[], bool],
        deadline: float,
    ) -> Any:
        complete_cancellable = getattr(self._inner, "complete_cancellable", None)
        if not callable(complete_cancellable):
            return self.complete(messages, tools)
        return self._complete_traced(
            messages,
            tools,
            lambda: complete_cancellable(
                messages,
                tools,
                is_cancelled=is_cancelled,
                deadline=deadline,
            ),
        )

    def _complete_traced(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        request: Callable[[], Any],
    ) -> Any:
        self._count += 1
        sequence = self._count
        self._writer.emit(
            "model_request_started",
            actor=self._actor,
            request_sequence=sequence,
            message_count=len(messages),
            tool_schema_count=len(tools),
            # The prompt is re-sent in full every turn, so its growth is the
            # single best live predictor of the prefill stall seen in step 2+.
            message_content_chars=sum(
                len(str(item.get("content") or "")) for item in messages
            ),
        )
        try:
            turn = request()
        except Exception as exc:
            self._writer.emit(
                "model_request_failed",
                actor=self._actor,
                request_sequence=sequence,
                error_type=type(exc).__name__,
                **(
                    {"error": str(exc)}
                    if self._writer.include_sensitive_content
                    else {}
                ),
            )
            raise
        self._writer.emit(
            "model_request_finished",
            actor=self._actor,
            request_sequence=sequence,
            finish_reason=turn.finish_reason,
            time_to_first_delta_seconds=turn.time_to_first_delta_seconds,
            generation_seconds=turn.generation_seconds,
            request_seconds=turn.request_seconds,
            request_started_at=turn.request_started_at,
            first_delta_at=turn.first_delta_at,
            terminal_at=turn.terminal_at,
            completed_at=turn.completed_at,
            prompt_tokens=turn.prompt_tokens,
            completion_tokens=turn.completion_tokens,
            content_chars=len(turn.content),
            content_sha256=_text_digest(turn.content),
            tool_calls=[
                {
                    "id": call.get("id", ""),
                    "name": call.get("function", {}).get("name", ""),
                    "arguments_chars": len(
                        call.get("function", {}).get("arguments", "")
                    ),
                    "arguments_sha256": _text_digest(
                        call.get("function", {}).get("arguments", "")
                    ),
                    **(
                        {
                            "arguments": call.get("function", {}).get(
                                "arguments", ""
                            )
                        }
                        if self._writer.include_sensitive_content
                        else {}
                    ),
                }
                for call in turn.tool_calls
            ],
            **(
                {"content_preview": turn.content[:CONTENT_PREVIEW_CHARS]}
                if self._writer.include_sensitive_content
                else {}
            ),
        )
        return turn


class TracingToolPlane:
    """Wrap the `execute_tool` callable, bracketing every tool dispatch."""

    def __init__(
        self,
        inner: Callable[[str, dict[str, Any]], str],
        writer: TraceWriter,
        *,
        actor: str,
    ) -> None:
        self._inner = inner
        self._writer = writer
        self._actor = actor
        self._count = 0

    def __call__(self, name: str, arguments: dict[str, Any]) -> str:
        self._count += 1
        sequence = self._count
        started = time.monotonic()
        self._writer.emit(
            "tool_started",
            actor=self._actor,
            tool_sequence=sequence,
            tool=name,
            arguments_chars=len(
                json.dumps(arguments, sort_keys=True, separators=(",", ":"))
            ),
            arguments_sha256=_json_digest(arguments),
            **(
                {"arguments": arguments}
                if self._writer.include_sensitive_content
                else {}
            ),
        )
        try:
            result = self._inner(name, arguments)
        except Exception as exc:
            self._writer.emit(
                "tool_failed",
                actor=self._actor,
                tool_sequence=sequence,
                tool=name,
                duration_seconds=time.monotonic() - started,
                error_type=type(exc).__name__,
                **(
                    {"error": str(exc)}
                    if self._writer.include_sensitive_content
                    else {}
                ),
            )
            raise
        self._writer.emit(
            "tool_finished",
            actor=self._actor,
            tool_sequence=sequence,
            tool=name,
            duration_seconds=time.monotonic() - started,
            result_chars=len(result),
            result_sha256=_text_digest(result),
            **(
                {"result_preview": result[:RESULT_PREVIEW_CHARS]}
                if self._writer.include_sensitive_content
                else {}
            ),
        )
        return result


def trace_single(
    task: str,
    *,
    workspace: Path,
    state_dir: Path,
    writer: TraceWriter,
    config: LocalModelConfig,
    max_steps: int,
) -> int:
    client = TracingModelClient(LocalMLXClient(config), writer, actor="agent")
    loop = LocalAgentLoop(
        model=client,
        execute_tool=TracingToolPlane(ReadOnlyLocalTools(workspace), writer, actor="agent"),
        state_dir=state_dir,
        limits=AgentLimits(max_steps=max_steps),
    )
    writer.emit(
        "run_started",
        actor="agent",
        mode="single",
        **_sensitive_text_fields(writer, "task", task),
    )
    result = loop.run(task)
    writer.emit(
        "run_finished",
        actor="agent",
        run_id=result.run_id,
        status=result.status,
        **_sensitive_text_fields(writer, "reason", result.reason),
        steps=result.steps,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        answer_chars=len(result.answer),
    )
    print(f"trace: {writer.path}")
    print(f"run:   {result.run_id}  status={result.status}")
    print(result.answer)
    return 0 if result.status == "completed" else 2


def trace_team(
    goal: str,
    assignments: Iterable[AgentAssignment],
    *,
    workspace: Path,
    state_dir: Path,
    writer: TraceWriter,
    config: LocalModelConfig,
    max_workers: int,
    max_steps: int,
) -> int:
    def model_for(actor: str) -> TracingModelClient:
        return TracingModelClient(LocalMLXClient(config), writer, actor=actor)

    def tool_for(actor: str) -> TracingToolPlane:
        return TracingToolPlane(ReadOnlyLocalTools(workspace), writer, actor=actor)

    team = LocalAgentTeam(
        model_factory=lambda: LocalMLXClient(config),
        execute_tool=ReadOnlyLocalTools(workspace),
        state_dir=state_dir,
        limits=AgentLimits(max_steps=max_steps, max_seconds=240.0),
        max_workers=max_workers,
        model_factory_for_agent=model_for,
        tool_factory_for_agent=tool_for,
    )
    ordered = list(assignments)
    writer.emit(
        "run_started",
        actor="team",
        mode="team",
        **_sensitive_text_fields(writer, "task", goal),
        assignments=[item.agent_id for item in ordered],
    )
    result = team.run(goal=goal, assignments=ordered)
    writer.emit(
        "run_finished",
        actor="team",
        run_id=result.team_run_id,
        status=result.status,
        worker_run_ids={item.agent_id: item.run_id for item in result.evidence},
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )
    print(f"trace: {writer.path}")
    print(f"team:  {result.team_run_id}  status={result.status}")
    for item in result.evidence:
        print(f"  {item.agent_id:20} {item.status:10} run={item.run_id}")
    return 0 if result.status == "completed" else 2


DEMO_ASSIGNMENTS = (
    AgentAssignment(
        agent_id="git-observer",
        role="repository observer",
        task="Use the git tool once with action status and report the exact repository state.",
        acceptance=AcceptanceContract(required_tools=("git",)),
    ),
    AgentAssignment(
        agent_id="config-reader",
        role="configuration reader",
        task="Read jarvis_v2/config.py and report how it enforces loopback-only endpoints.",
        acceptance=AcceptanceContract(required_tools=("file",)),
    ),
    AgentAssignment(
        agent_id="model-reader",
        role="model client reader",
        task="Read jarvis_v2/model.py and report one way it validates the SSE stream.",
        acceptance=AcceptanceContract(required_tools=("file",)),
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", nargs="?", help="task for a single traced agent run")
    parser.add_argument("--team", action="store_true", help="run the demo concurrent team instead")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--trace-dir", type=Path, default=DEFAULT_TRACE_DIR)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8080/v1")
    parser.add_argument("--model", default="mlx-community/Qwen3-8B-4bit")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--max-workers", type=int, default=3)
    parser.add_argument(
        "--include-sensitive-content",
        action="store_true",
        help="store raw task, model, tool argument, result, and error previews",
    )
    args = parser.parse_args()

    if not args.task and not args.team:
        parser.error("provide a task or --team")

    try:
        config = LocalModelConfig(
            base_url=args.endpoint,
            model=args.model,
            max_output_tokens=1024,
            request_timeout_seconds=180.0,
        )
    except LocalConfigurationError as exc:
        print(f"invalid local model endpoint: {exc}", file=sys.stderr)
        return 2
    if not LocalMLXClient(config).ready():
        print(f"local MLX server is not ready at {config.base_url}", file=sys.stderr)
        return 2
    writer = TraceWriter(
        args.trace_dir,
        include_sensitive_content=args.include_sensitive_content,
    )

    try:
        if args.team:
            state_dir = args.state_dir or Path(".jarvis-v2/team-runs")
            return trace_team(
                args.task or "Report the current state of the Jarvis V2 runtime.",
                DEMO_ASSIGNMENTS,
                workspace=args.workspace,
                state_dir=state_dir,
                writer=writer,
                config=config,
                max_workers=args.max_workers,
                max_steps=min(args.max_steps, 4),
            )

        state_dir = args.state_dir or Path(".jarvis-v2/runs")
        return trace_single(
            args.task,
            workspace=args.workspace,
            state_dir=state_dir,
            writer=writer,
            config=config,
            max_steps=args.max_steps,
        )
    except Exception as exc:
        writer.emit(
            "run_finished",
            actor="launcher",
            status="failed",
            error_type=type(exc).__name__,
            **(
                {"error": str(exc)}
                if writer.include_sensitive_content
                else {}
            ),
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
