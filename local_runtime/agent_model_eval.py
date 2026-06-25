"""Non-mutating tool-protocol evaluation for local Ollama candidates."""

from __future__ import annotations
import logging

import argparse
import json
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from brains import brain_ollama
from local_runtime import glm52_readiness
import usage_tracker


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read one project file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_subtask",
            "description": "Assign one scoped task to a specialist agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string"},
                    "task": {"type": "string"},
                },
                "required": ["agent", "task"],
            },
        },
    },
]


@dataclass(frozen=True)
class ProtocolCase:
    id: str
    prompt: str
    expected_tools: tuple[str, ...]
    expected_agents: tuple[str, ...] = ()
    expected_paths: tuple[str, ...] = ()
    expected_task_terms: tuple[tuple[str, str], ...] = ()
    require_visible_answer: bool = False
    simulate_tool_result: bool = False


DEFAULT_CASES = (
    ProtocolCase(
        id="single_file_tool",
        prompt="Inspect config.py before answering. Use the file tool exactly once.",
        expected_tools=("read_file",),
        expected_paths=("config.py",),
        simulate_tool_result=True,
    ),
    ProtocolCase(
        id="parallel_specialists",
        prompt=(
            "Delegate the implementation review to backend_engineer and the threat review "
            "to security_reviewer in parallel. Do not perform either task yourself."
        ),
        expected_tools=("delegate_subtask", "delegate_subtask"),
        expected_agents=("backend_engineer", "security_reviewer"),
        expected_task_terms=(("backend_engineer", "implementation"), ("security_reviewer", "threat")),
    ),
    ProtocolCase(
        id="plan_without_tools",
        prompt="Give a three-step evaluation plan in prose. Do not call any tool.",
        expected_tools=(),
        require_visible_answer=True,
    ),
)


def _is_cloud_model(model: str) -> bool:
    tag = (model or "").strip().lower().rsplit(":", 1)[-1]
    return "cloud" in {part for part in tag.replace("_", "-").replace(".", "-").split("-") if part}


def _normalize_tag(model: str) -> str:
    value = (model or "").strip()
    return value[:-7] if value.endswith(":latest") else value


def _value(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _tool_calls(message: Any) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for call in _value(message, "tool_calls", []) or []:
        function = _value(call, "function", {})
        name = str(_value(function, "name", "") or "")
        arguments = _value(function, "arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = None
        parsed.append({"name": name, "arguments": arguments})
    return parsed


def _schema_is_valid(calls: list[dict[str, Any]]) -> bool:
    required = {
        "read_file": {"path"},
        "delegate_subtask": {"agent", "task"},
    }
    for call in calls:
        name = call["name"]
        arguments = call["arguments"]
        if name not in required or not isinstance(arguments, dict):
            return False
        if not required[name].issubset(arguments):
            return False
        if any(not isinstance(arguments[key], str) or not arguments[key].strip() for key in required[name]):
            return False
    return True


def _record_usage(response: Any, model: str, case_id: str, phase: str) -> None:
    prompt_tokens = _value(response, "prompt_eval_count")
    completion_tokens = _value(response, "eval_count")
    try:
        usage_tracker.record(
            provider="ollama",
            model=model,
            local=brain_ollama._ollama_usage_is_local(),
            source="local_runtime.agent_model_eval",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=(
                int(prompt_tokens) + int(completion_tokens)
                if prompt_tokens is not None and completion_tokens is not None else None
            ),
            estimated=prompt_tokens is None or completion_tokens is None,
            metadata={
                "protocol_eval": True,
                "case_id": case_id,
                "phase": phase,
                "endpoint_scope": brain_ollama._ollama_endpoint_scope(),
            },
        )
    except Exception:
        logging.debug("[AgentModelEval] silent failure in _record_usage", exc_info=True)


def _run_case(client: Any, model: str, case: ProtocolCase) -> dict[str, Any]:
    started = time.perf_counter()
    response = client.chat(
        model=model,
        messages=[
            {"role": "system", "content": "Follow the requested tool protocol exactly."},
            {"role": "user", "content": case.prompt},
        ],
        tools=TOOL_SCHEMAS,
        stream=False,
    )
    _record_usage(response, model, case.id, "decision")
    message = _value(response, "message", {})
    calls = _tool_calls(message)
    names = [call["name"] for call in calls]
    agents = [
        call["arguments"].get("agent")
        for call in calls
        if call["name"] == "delegate_subtask" and isinstance(call["arguments"], dict)
    ]
    content = str(_value(message, "content", "") or "")
    thinking = str(_value(message, "thinking", "") or "")
    paths = [
        call["arguments"].get("path")
        for call in calls
        if call["name"] == "read_file" and isinstance(call["arguments"], dict)
    ]
    schema_valid = _schema_is_valid(calls)
    tools_match = Counter(names) == Counter(case.expected_tools)
    agents_match = Counter(agents) == Counter(case.expected_agents)
    paths_match = Counter(paths) == Counter(case.expected_paths)
    task_terms_match = all(
        any(
            call["name"] == "delegate_subtask"
            and isinstance(call["arguments"], dict)
            and call["arguments"].get("agent") == agent
            and term.lower() in str(call["arguments"].get("task", "")).lower()
            for call in calls
        )
        for agent, term in case.expected_task_terms
    )
    continuation_ok = True
    followup_prompt_tokens = 0
    followup_completion_tokens = 0
    if case.simulate_tool_result and schema_valid and tools_match:
        followup = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": "Follow the requested tool protocol exactly."},
                {"role": "user", "content": case.prompt},
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {"function": {"name": call["name"], "arguments": call["arguments"]}}
                        for call in calls
                    ],
                },
                {"role": "tool", "content": "Synthetic file result: LOCAL_DEFAULT is configured."},
            ],
            tools=TOOL_SCHEMAS,
            stream=False,
        )
        _record_usage(followup, model, case.id, "continuation")
        followup_message = _value(followup, "message", {})
        continuation_ok = bool(str(_value(followup_message, "content", "") or "").strip())
        continuation_ok = continuation_ok and not _tool_calls(followup_message)
        thinking += str(_value(followup_message, "thinking", "") or "")
        followup_prompt_tokens = int(_value(followup, "prompt_eval_count", 0) or 0)
        followup_completion_tokens = int(_value(followup, "eval_count", 0) or 0)
    visible_answer_ok = not case.require_visible_answer or bool(content.strip())
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    passed = all((
        schema_valid,
        tools_match,
        agents_match,
        paths_match,
        task_terms_match,
        continuation_ok,
        visible_answer_ok,
    ))
    return {
        "case_id": case.id,
        "passed": passed,
        "schema_valid": schema_valid,
        "tools_match": tools_match,
        "agents_match": agents_match,
        "argument_semantics_match": paths_match and task_terms_match,
        "continuation_ok": continuation_ok,
        "visible_answer_ok": visible_answer_ok,
        "tool_names": names,
        "delegated_agents": agents,
        "latency_ms": latency_ms,
        "prompt_tokens": int(_value(response, "prompt_eval_count", 0) or 0) + followup_prompt_tokens,
        "completion_tokens": int(_value(response, "eval_count", 0) or 0) + followup_completion_tokens,
        "visible_output_chars": len(content),
        "thinking_chars": len(thinking),
    }


def run_eval(
    model: str,
    *,
    client: Any | None = None,
    cases: tuple[ProtocolCase, ...] | list[ProtocolCase] | None = None,
    expected_digest: str | None = None,
) -> dict[str, Any]:
    """Evaluate protocol behavior without executing tools or changing promotion state."""
    requested = _normalize_tag(model)
    if not requested or _is_cloud_model(requested):
        return {"ok": False, "promotion_ready": False, "error": "A non-cloud model is required."}
    installed = [_normalize_tag(item) for item in brain_ollama.list_local_models()]
    if requested not in installed:
        return {
            "ok": False,
            "promotion_ready": False,
            "error": f"Exact model is not visible on the configured Ollama endpoint: {model}",
            "installed_models": installed,
        }

    protocol_cases = tuple(cases or DEFAULT_CASES)
    target_client = client or brain_ollama._client()
    digest_check = glm52_readiness.validate_candidate_digest(
        target_client,
        model,
        expected_digest=expected_digest,
    )
    if not digest_check["ok"]:
        return {"ok": False, "promotion_ready": False, "error": digest_check["error"]}
    results: list[dict[str, Any]] = []
    try:
        for case in protocol_cases:
            results.append(_run_case(target_client, model, case))
    except Exception as exc:
        return {
            "ok": False,
            "promotion_ready": False,
            "error": f"Protocol evaluation failed: {type(exc).__name__}",
            "results": results,
        }

    count = len(results)
    passes = sum(1 for result in results if result["passed"])
    schema_valid = sum(1 for result in results if result["schema_valid"])
    pass_rate = round(passes / count, 4) if count else 0.0
    schema_valid_rate = round(schema_valid / count, 4) if count else 0.0
    hard_gates_passed = bool(count) and pass_rate == 1.0 and schema_valid_rate == 1.0
    return {
        "ok": True,
        "model": model,
        "non_mutating": True,
        "scope": "synthetic_tool_protocol_only",
        "nested_subagents_executed": False,
        "tools_executed": 0,
        "case_count": count,
        "passes": passes,
        "pass_rate": pass_rate,
        "schema_valid_rate": schema_valid_rate,
        "thinking_chars": sum(result["thinking_chars"] for result in results),
        "protocol_ready": hard_gates_passed,
        "promotion_ready": False,
        "promotion_note": "Protocol success is not a production promotion decision.",
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the non-mutating local agent protocol eval.")
    parser.add_argument("model", help="Exact non-cloud model visible on the configured Ollama endpoint")
    args = parser.parse_args()
    result = run_eval(args.model)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
