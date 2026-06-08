"""
agents/researcher.py
Worker module for the Researcher agent.

Wraps research.deep_research() — the web-search + page-fetch + synthesis pipeline —
into the standard process_task() / event-bus worker contract.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from config import AGENT_ROSTER

logger = logging.getLogger("jarvis.agent.researcher")

try:
    from infra.memory import store
except ImportError:
    store = None
    logger.warning("infra.memory.store unavailable; vector memory disabled.")

deep_research = None
_RESEARCH_AVAILABLE = False


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _cloud_research_allowed(task_context: dict[str, Any]) -> bool:
    """Cloud research requires an operator-enabled flag and task-level approval."""
    env_allowed = _truthy(os.getenv("ALLOW_CLOUD_RESEARCH")) or _truthy(
        os.getenv("JARVIS_ALLOW_CLOUD_RESEARCH")
    )
    task_approved = _truthy(task_context.get("cloud_research_approved"))
    return env_allowed and task_approved


def _load_deep_research():
    """Import cloud-backed research lazily so local-first mode never imports Claude."""
    global deep_research, _RESEARCH_AVAILABLE
    if deep_research is not None:
        _RESEARCH_AVAILABLE = True
        return deep_research
    try:
        from research import deep_research as loaded_deep_research
    except Exception as exc:
        _RESEARCH_AVAILABLE = False
        logger.warning("research.py unavailable; using local synthesis: %s", exc)
        return None
    deep_research = loaded_deep_research
    _RESEARCH_AVAILABLE = True
    return deep_research


def _fallback_synthesis(description: str, model: str, system_prompt: str) -> str:
    """LLM-only answer when research pipeline is unavailable."""
    from brains.brain_ollama import ask_local_stream
    chunks = list(ask_local_stream(description, model=model, system_extra=system_prompt))
    return "".join(chunks)


def process_task(payload: dict[str, Any]) -> str:
    """
    Process a research task.

    1. Recalls relevant memory context.
    2. Runs deep_research() (web search + page fetch + LLM synthesis).
    3. Formats the report with sources.
    4. Writes the report back to the event bus /results endpoint.
    """
    task_id = payload.get("task_id", "")
    title = payload.get("title", "")
    description = payload.get("description", "")
    task_context = payload.get("context", {})
    depth = int(task_context.get("research_depth", 3))

    logger.info("Processing task_id=%s title=%r depth=%d", task_id, title, depth)

    memory_context = ""
    if store:
        try:
            memory_context = store.context_for_prompt(
                query=f"{title} {description}".strip(),
                top_k=3,
                session_id=task_context.get("session_id") or None,
                project_id=task_context.get("project_id") or None,
                max_chars=1500,
            )
        except Exception as exc:
            logger.warning("Memory recall failed: %s", exc)

    roster_config = AGENT_ROSTER.get("researcher", {})
    model = roster_config.get("model", "glm-4.7-flash")
    system_prompt = roster_config.get("system_prompt", "")

    # Build the research topic — combine title + description + any prior memory context
    topic = description or title
    if memory_context:
        topic = f"{topic}\n\nRelevant prior context:\n{memory_context}"

    output = ""
    research_fn = _load_deep_research() if _cloud_research_allowed(task_context) else None
    if research_fn is not None:
        try:
            result = research_fn(topic, depth=depth)
            report = result.get("report", "")
            sources = result.get("sources", [])
            queries_used = result.get("queries_used", [])

            source_lines = "\n".join(
                f"- [{s.get('title', s.get('url', ''))}]({s.get('url', '')})"
                for s in sources[:10]
            )
            output = f"{report}\n\n**Sources**\n{source_lines}"
            if queries_used:
                output += f"\n\n**Queries used:** {', '.join(queries_used)}"
        except Exception as exc:
            logger.error("deep_research failed: %s", exc)
            output = f"Research pipeline error: {exc}"
    else:
        output = _fallback_synthesis(topic, model, system_prompt)

    event_bus_url = os.getenv("EVENT_BUS_URL", "http://localhost:8766").rstrip("/")
    if task_id:
        try:
            httpx.post(
                f"{event_bus_url}/results",
                json={"task_id": task_id, "output": output, "needs_review": False},
                headers={"X-Jarvis-Agent-ID": "researcher"},
                timeout=5.0,
            )
        except Exception as exc:
            logger.warning("Could not post results to event bus: %s", exc)

    return output
