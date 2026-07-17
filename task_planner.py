from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from brains.brain_claude import ask_claude
from config import DEFAULT_MODE, LOCAL_REASONING, SONNET
import skills
import tool_registry

log = logging.getLogger(__name__)

_LOCAL_PLANNER_NUM_CTX = 32_768
_LOCAL_PLANNER_FALLBACK_NUM_CTX = 8_192
_LOCAL_PLANNER_NUM_PREDICT = 1_024
_LOCAL_PLANNER_READ_TIMEOUT_SECONDS = 60.0
_LOCAL_PLANNER_MAX_TASK_CHARS = 12_000
_LOCAL_PLANNER_MAX_RECOVERY_CONTEXT_CHARS = 8_000
_LOCAL_PLANNER_MAX_ERROR_CHARS = 2_000


@dataclass
class TaskStep:
    number: int
    description: str
    tool: str
    params: dict = field(default_factory=dict)
    result: str = ""
    ok: bool = False


# Cloud planning prompt (unchanged from original, extended step cap)
_PLAN_SYSTEM = """You are Jarvis's task planner. Break a user's goal into sequential steps.

Callable tools (use these explicitly whenever possible):
{tool_summaries}

Return ONLY a valid JSON array of steps. Each step:
{{
  "number": 1,
  "description": "what this step does",
  "tool": "<tool name>",
  "params": {{"key": "value"}}
}}

Rules:
- Use the minimum steps needed
- Pass outputs from one step to the next via params where needed (use placeholder: "$step_N_result")
- Maximum 12 steps
- Prefer tool-backed steps for factual/system actions; use "chat" only for synthesis/rewrite/reasoning text
- Use exact tool names from the callable tool list
- If unclear, ask via a single "chat" step"""


# Local planning prompt — tight JSON-only instructions for qwen3:30b-a3b
_PLAN_SYSTEM_LOCAL = """You are a task planning agent. Break the given goal into sequential executable steps.

Available tools:
{tool_summaries}

Output ONLY a valid JSON object — no markdown, no code blocks, no explanation.
Exact format:
{{"steps": [{{"number": 1, "description": "what this step does", "tool": "tool_name", "params": {{"key": "value"}}}}, ...]}}

Rules:
- Use exact tool names from the list above; "chat" only when no specific tool fits
- Maximum 12 steps, minimum 1
- Chain outputs with "$step_N_result" in params
- Output ONLY the JSON object, nothing else"""


def _extract_json_steps(raw: str) -> list[dict]:
    """Parse step data from raw model output. Handles markdown, wrapper objects, bare arrays."""
    text = raw.strip()

    # Strip markdown code fences (brain_ollama._strip_markdown already removes ``` markers
    # but leaves content; handle both stripped and unstripped cases)
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:]).strip()
        if text.endswith("```"):
            text = text[:-3].strip()

    # Try {"steps": [...]} wrapper
    obj_match = re.search(r'\{.*\}', text, re.DOTALL)
    if obj_match:
        try:
            parsed = json.loads(obj_match.group())
            if isinstance(parsed, dict) and "steps" in parsed:
                data = parsed["steps"]
                if isinstance(data, list) and data:
                    return data
            elif isinstance(parsed, dict) and any(k in parsed for k in ("number", "tool")):
                return [parsed]
        except json.JSONDecodeError:
            pass

    # Try bare array
    arr_match = re.search(r'\[.*\]', text, re.DOTALL)
    if arr_match:
        try:
            data = json.loads(arr_match.group())
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    raise ValueError(f"No valid JSON plan in response (first 200 chars): {text[:200]}")


def _build_steps(data: list[dict], *, max_steps: int = 12) -> list[TaskStep]:
    if not data:
        raise ValueError("Plan must contain at least one step")

    steps: list[TaskStep] = []
    for idx, item in enumerate(data[:max_steps]):
        if not isinstance(item, dict):
            raise ValueError(f"Plan step {idx + 1} must be an object")
        params = item.get("params", {})
        if not isinstance(params, dict):
            raise ValueError(f"Plan step {idx + 1} params must be an object")
        tool = str(item.get("tool", "chat")).strip().lower() or "chat"
        if tool_registry.get_tool_spec(tool) is None:
            tool = "chat"
        steps.append(TaskStep(
            number=int(item.get("number", idx + 1)),
            description=str(item.get("description", "")),
            tool=tool,
            params=dict(params),
        ))
    return steps


def _local_planner_options(model: str) -> dict[str, int]:
    """Return deterministic Ollama limits for JSON planning requests."""
    from brains.brain_ollama import _ollama_options_for_model

    options = _ollama_options_for_model(model)
    configured_ctx = int(options.get("num_ctx", _LOCAL_PLANNER_FALLBACK_NUM_CTX))
    configured_predict = int(options.get("num_predict", _LOCAL_PLANNER_NUM_PREDICT))
    if configured_ctx <= 0 or configured_predict <= 0:
        raise ValueError("Local planner token limits must be positive integers")
    options["num_ctx"] = min(configured_ctx, _LOCAL_PLANNER_NUM_CTX)
    options["num_predict"] = min(configured_predict, _LOCAL_PLANNER_NUM_PREDICT)
    options["temperature"] = 0
    return options


def _local_plan_schema(max_steps: int) -> dict:
    """Constrain local planner output to the shape consumed by _build_steps."""
    return {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": max_steps,
                "items": {
                    "type": "object",
                    "properties": {
                        "number": {"type": "integer"},
                        "description": {"type": "string"},
                        "tool": {"type": "string"},
                        "params": {"type": "object"},
                    },
                    "required": ["number", "description", "tool", "params"],
                },
            },
        },
        "required": ["steps"],
    }


def _plan_task_local(task: str) -> list[TaskStep]:
    """Plan with LOCAL_REASONING. Up to 3 parse retries.

    Uses a dedicated Ollama client with planning-appropriate timeouts:
    - 30s connect/pool (model may still be loading)
    - 60s read per attempt (three attempts remain bounded to about three minutes)
    Does not inherit the singleton's short timeouts. Planning requests use a
    bounded context and disable reasoning output so JSON arrives in content.
    """
    import ollama as _ollama_lib
    from brains.brain_ollama import get_best_available

    try:
        import httpx
        _timeout = httpx.Timeout(
            connect=30.0,
            read=_LOCAL_PLANNER_READ_TIMEOUT_SECONDS,
            write=30.0,
            pool=30.0,
        )
        client = _ollama_lib.Client(timeout=_timeout)
    except ImportError:
        client = _ollama_lib.Client(timeout=_LOCAL_PLANNER_READ_TIMEOUT_SECONDS)

    model = get_best_available(LOCAL_REASONING)
    options = _local_planner_options(model)
    system = _PLAN_SYSTEM_LOCAL.format(
        tool_summaries=tool_registry.callable_tool_summaries()
    )
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": f"Plan this task: {task[:_LOCAL_PLANNER_MAX_TASK_CHARS]}",
        },
    ]

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = client.chat(
                model=model,
                messages=messages,
                stream=False,
                think=False,
                format=_local_plan_schema(max_steps=12),
                options=options,
            )
            raw = (response.message.content or "").strip()
            data = _extract_json_steps(raw)
            steps = _build_steps(data)
            log.info("[Planner] Local planning succeeded on attempt %d — %d steps", attempt + 1, len(steps))
            return steps
        except Exception as exc:
            last_error = exc
            log.warning("[Planner] Local attempt %d/3 failed: %s", attempt + 1, exc)

    raise RuntimeError(f"Local planner failed after 3 attempts: {last_error}") from last_error


_REPLAN_SYSTEM_LOCAL = """You are a task recovery agent. A step in an ongoing plan failed.
Your job: produce 1-3 corrective steps that fix or work around the failure and resume the task.

Available tools:
{tool_summaries}

Output ONLY a valid JSON object — no markdown, no code blocks, no explanation.
Exact format:
{{"steps": [{{"number": 1, "description": "corrective action", "tool": "tool_name", "params": {{"key": "value"}}}}, ...]}}

Rules:
- Maximum 3 corrective steps
- Address the specific error, don't re-do already completed steps
- Use exact tool names; "chat" for reasoning/retry
- Output ONLY the JSON object, nothing else"""


def replan_after_failure(
    task: str,
    completed_steps: list[TaskStep],
    failed_step: TaskStep,
    error: str,
) -> list[TaskStep] | None:
    """Ask the local planner for 1-3 corrective steps after a step failure.

    Returns corrective steps to append, or None if replanning itself fails.
    Step numbers are offset to continue after the failed step.
    """
    try:
        import ollama as _ollama_lib
        from brains.brain_ollama import get_best_available

        try:
            import httpx
            _timeout = httpx.Timeout(
                connect=30.0,
                read=_LOCAL_PLANNER_READ_TIMEOUT_SECONDS,
                write=30.0,
                pool=30.0,
            )
            client = _ollama_lib.Client(timeout=_timeout)
        except ImportError:
            client = _ollama_lib.Client(timeout=_LOCAL_PLANNER_READ_TIMEOUT_SECONDS)

        model = get_best_available(LOCAL_REASONING)
        options = _local_planner_options(model)
        completed_desc = "\n".join(
            f"  Step {s.number} [{s.tool}]: {s.description} — OK"
            for s in completed_steps
            if s.ok
        )[:_LOCAL_PLANNER_MAX_RECOVERY_CONTEXT_CHARS]
        prompt = (
            f"Original task: {task[:_LOCAL_PLANNER_MAX_TASK_CHARS]}\n\n"
            f"Completed so far:\n{completed_desc or '  (none)'}\n\n"
            f"Failed step {failed_step.number} [{failed_step.tool}]: {failed_step.description}\n"
            f"Error: {error[:_LOCAL_PLANNER_MAX_ERROR_CHARS]}\n\n"
            "Produce corrective steps to recover and complete the task."
        )
        system = _REPLAN_SYSTEM_LOCAL.format(
            tool_summaries=tool_registry.callable_tool_summaries()
        )
        response = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            stream=False,
            think=False,
            format=_local_plan_schema(max_steps=3),
            options=options,
        )
        raw = (response.message.content or "").strip()
        data = _extract_json_steps(raw)
        corrective = _build_steps(data, max_steps=3)
        # Renumber to continue after failed step
        offset = failed_step.number
        for i, s in enumerate(corrective):
            s.number = offset + i + 1
        log.info("[Planner] Replanned %d corrective steps after step %d failure",
                 len(corrective), failed_step.number)
        return corrective
    except Exception as exc:
        log.warning("[Planner] replan_after_failure failed: %s", exc)
        return None


def plan_task(task: str) -> list[TaskStep]:
    system_extra, _ = skills.build_system_extra(task, skill_id="planning_execution", tool="chat")

    # Use local model unless explicitly in cloud-only mode
    use_cloud_only = DEFAULT_MODE == "cloud"

    if not use_cloud_only:
        try:
            return _plan_task_local(task)
        except Exception as exc:
            log.warning("[Planner] Local planning failed: %s", exc)
            if DEFAULT_MODE in ("local", "open-source"):
                # Cloud fallback blocked — return single-step degraded plan
                log.warning("[Planner] Cloud blocked (mode=%s); returning single-step plan.", DEFAULT_MODE)
                return [TaskStep(1, f"Execute: {task}", "chat", {"prompt": task})]
            # "auto" mode: fall through to cloud

    # Cloud path
    try:
        raw = ask_claude(
            f"Plan this task: {task}",
            model=SONNET,
            system=_PLAN_SYSTEM.format(tool_summaries=tool_registry.callable_tool_summaries()),
            system_extra=system_extra,
        ).strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:])
            if raw.endswith("```"):
                raw = raw[:-3]
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            raise ValueError("No JSON array in response")
        data = json.loads(match.group())
        return _build_steps(data)
    except Exception:
        return [TaskStep(1, f"Execute: {task}", "chat", {"prompt": task})]
