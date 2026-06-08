"""
Cloud brief to local Jarvis work order conversion.

This module is intentionally local-only. It does not call cloud providers or
LLMs; it turns a pasted Claude/ChatGPT/Gemini plan into auditable local tasks
that Jarvis Manager can review before any worker executes them.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any

from core.manager import AgentTask, _task_requires_manager_gate


_CORE_AGENT_NAMES = {
    "backend_engineer",
    "frontend_designer",
    "ux_researcher",
    "security_reviewer",
    "qa_tester",
    "researcher",
    "devops_release",
    "memory_librarian",
}

_RISK_TERMS = {
    "api key",
    "anthropic",
    "callback",
    "chatgpt",
    "claude",
    "cloud model",
    "cloud research",
    "curl",
    "credential",
    "delete",
    "deploy",
    "gemini",
    "external api",
    "git push",
    "http",
    "internet",
    "openai",
    "production",
    "secret",
    "submit",
    "third-party",
    "web search",
}

_RESERVED_CONTEXT_KEYS = {
    "allow_cloud_research",
    "approval_id",
    "approval_token",
    "approved",
    "approved_by",
    "cloud_research_approved",
    "dispatch",
    "execute",
    "requires_human_review",
    "source",
    "status",
}


def _valid_agents() -> set[str]:
    try:
        from config import AGENT_ROSTER

        return _CORE_AGENT_NAMES | set(AGENT_ROSTER)
    except Exception:
        return set(_CORE_AGENT_NAMES)


def _normalize_agent(agent: str, valid_agents: set[str]) -> str:
    name = (agent or "").strip().replace("-", "_")
    return name if name in valid_agents else "researcher"


def _looks_risky(text: str) -> bool:
    lower = text.lower()
    return any(term in lower for term in _RISK_TERMS)


def _truthy_context(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _sanitize_context(context: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in context.items()
        if str(key).lower() not in _RESERVED_CONTEXT_KEYS
    }


def _coerce_priority(value: Any) -> int:
    try:
        return max(1, min(10, int(value)))
    except (TypeError, ValueError):
        return 5


def _task_to_dict(task: AgentTask) -> dict[str, Any]:
    data = asdict(task)
    data.pop("task_id", None)
    data.pop("status", None)
    data.pop("security_verdict", None)
    data["review_required"] = bool(
        data.get("needs_security_review")
        or data.get("context", {}).get("requires_human_review")
    )
    return data


def _mark_review_if_needed(task: AgentTask) -> AgentTask:
    if _task_requires_manager_gate(task):
        task.needs_security_review = True
        task.context["requires_human_review"] = True
    return task


def _from_mapping(item: dict[str, Any], *, source: str, valid_agents: set[str]) -> AgentTask:
    title = str(item.get("title") or item.get("name") or "Cloud brief task")[:120]
    description = str(item.get("description") or item.get("task") or title)
    raw_context = item.get("context") if isinstance(item.get("context"), dict) else {}
    context = _sanitize_context(raw_context)
    requested_cloud_research = _truthy_context(raw_context.get("allow_cloud_research")) or _truthy_context(
        raw_context.get("cloud_research_approved")
    )
    context = {
        **context,
        "source": "cloud_brief",
        "cloud_source": source,
    }
    if requested_cloud_research:
        context["requested_cloud_research"] = True
    task = AgentTask(
        title=title,
        description=description,
        agent=_normalize_agent(str(item.get("agent") or "researcher"), valid_agents),
        priority=_coerce_priority(item.get("priority", 5)),
        needs_security_review=bool(item.get("needs_security_review", False))
        or requested_cloud_research
        or _looks_risky(f"{title}\n{description}"),
        context=context,
    )
    return _mark_review_if_needed(task)


def _parse_json_tasks(brief: str, *, source: str, valid_agents: set[str]) -> list[AgentTask]:
    text = brief.strip()
    if not text:
        return []
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        candidates.append(text[start:end])

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        raw_tasks = data.get("tasks") if isinstance(data, dict) else data
        if not isinstance(raw_tasks, list):
            continue
        return [
            _from_mapping(item, source=source, valid_agents=valid_agents)
            for item in raw_tasks[:6]
            if isinstance(item, dict)
        ]
    return []


_BULLET_RE = re.compile(
    r"^\s*(?:[-*]|\d+[.)])\s*(?:`?(?P<agent>[a-zA-Z_][\w-]*)`?\s*[:\-])?\s*(?P<body>.+?)\s*$"
)


def _parse_bullets(brief: str, *, source: str, valid_agents: set[str]) -> list[AgentTask]:
    tasks: list[AgentTask] = []
    for line in brief.splitlines():
        match = _BULLET_RE.match(line)
        if not match:
            continue
        body = match.group("body").strip()
        if not body:
            continue
        agent = _normalize_agent(match.group("agent") or "researcher", valid_agents)
        task = AgentTask(
            title=body[:80],
            description=body,
            agent=agent,
            priority=5,
            needs_security_review=_looks_risky(body),
            context={"source": "cloud_brief", "cloud_source": source},
        )
        tasks.append(_mark_review_if_needed(task))
        if len(tasks) >= 6:
            break
    return tasks


def tasks_from_cloud_brief(brief: str, *, source: str = "cloud") -> list[AgentTask]:
    """
    Convert a pasted cloud-model brief into local Jarvis task objects.

    Supported input:
    - JSON object/list with task dictionaries.
    - Markdown/plain bullets, optionally prefixed by an agent id.
    - Fallback: one researcher task to turn the brief into a local execution plan.
    """
    clean = (brief or "").strip()
    if not clean:
        return []

    valid_agents = _valid_agents()
    tasks = _parse_json_tasks(clean, source=source, valid_agents=valid_agents)
    if not tasks:
        tasks = _parse_bullets(clean, source=source, valid_agents=valid_agents)
    if not tasks:
        tasks = [
            AgentTask(
                title="Convert cloud brief into local execution plan",
                description=clean,
                agent="researcher",
                priority=5,
                needs_security_review=_looks_risky(clean),
                context={"source": "cloud_brief", "cloud_source": source},
            )
        ]
        _mark_review_if_needed(tasks[0])
    return tasks


def preview_work_order(brief: str, *, source: str = "cloud") -> dict[str, Any]:
    tasks = tasks_from_cloud_brief(brief, source=source)
    return {
        "tasks": [_task_to_dict(task) for task in tasks],
        "task_count": len(tasks),
        "review_required_count": sum(
            1
            for task in tasks
            if task.needs_security_review or task.context.get("requires_human_review")
        ),
        "execute": False,
        "note": "Preview only. Submit through Jarvis Manager after human review.",
    }
