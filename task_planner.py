from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from brains.brain_claude import ask_claude
from config import SONNET
import skills
import tool_registry


@dataclass
class TaskStep:
    number: int
    description: str
    tool: str
    params: dict = field(default_factory=dict)
    result: str = ""
    ok: bool = False


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
- Maximum 6 steps
- Prefer tool-backed steps for factual/system actions; use "chat" only for synthesis/rewrite/reasoning text
- Use exact tool names from the callable tool list
- If unclear, ask via a single "chat" step"""


def plan_task(task: str) -> list[TaskStep]:
    system_extra, _ = skills.build_system_extra(task, skill_id="planning_execution", tool="chat")
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
