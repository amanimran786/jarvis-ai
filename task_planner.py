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


def _build_steps(data: list[dict]) -> list[TaskStep]:
    steps: list[TaskStep] = []
    for idx, item in enumerate(data):
        tool = str(item.get("tool", "chat")).strip().lower() or "chat"
        if tool_registry.get_tool_spec(tool) is None:
            tool = "chat"
        steps.append(TaskStep(
            number=int(item.get("number", idx + 1)),
            description=str(item.get("description", "")),
            tool=tool,
            params=dict(item.get("params", {})),
        ))
    return steps


def _plan_task_local(task: str) -> list[TaskStep]:
    """Plan with LOCAL_REASONING (qwen3:30b-a3b). Up to 3 parse retries."""
    from brains.brain_ollama import ask_local

    system_extra = _PLAN_SYSTEM_LOCAL.format(
        tool_summaries=tool_registry.callable_tool_summaries()
    )
    prompt = f"Plan this task: {task}"

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            raw = ask_local(
                prompt,
                model=LOCAL_REASONING,
                system_extra=system_extra,
                include_memory=False,
                raise_on_error=True,
            )
            data = _extract_json_steps(raw)
            return _build_steps(data)
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
        from brains.brain_ollama import ask_local

        completed_desc = "\n".join(
            f"  Step {s.number} [{s.tool}]: {s.description} — OK"
            for s in completed_steps
            if s.ok
        )
        prompt = (
            f"Original task: {task}\n\n"
            f"Completed so far:\n{completed_desc or '  (none)'}\n\n"
            f"Failed step {failed_step.number} [{failed_step.tool}]: {failed_step.description}\n"
            f"Error: {error[:300]}\n\n"
            "Produce corrective steps to recover and complete the task."
        )
        system = _REPLAN_SYSTEM_LOCAL.format(
            tool_summaries=tool_registry.callable_tool_summaries()
        )
        raw = ask_local(
            prompt,
            model=LOCAL_REASONING,
            system_extra=system,
            include_memory=False,
            raise_on_error=True,
        )
        data = _extract_json_steps(raw)
        corrective = _build_steps(data)
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
        steps: list[TaskStep] = []
        for idx, item in enumerate(data):
            tool = str(item.get("tool", "chat")).strip().lower() or "chat"
            if tool_registry.get_tool_spec(tool) is None:
                tool = "chat"
            steps.append(
                TaskStep(
                    number=int(item.get("number", idx + 1)),
                    description=str(item.get("description", "")),
                    tool=tool,
                    params=dict(item.get("params", {})),
                )
            )
        return steps
    except Exception:
        return [TaskStep(1, f"Execute: {task}", "chat", {"prompt": task})]
