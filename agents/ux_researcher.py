"""
agents/ux_researcher.py
Worker module for the UX Researcher agent.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from brains.brain_ollama import ask_local_with_tools
from config import AGENT_ROSTER

logger = logging.getLogger("jarvis.agent.ux_researcher")

try:
    from infra.memory import store
except ImportError:
    store = None
    logger.warning("infra.memory.store unavailable; vector memory disabled.")


def process_task(payload: dict[str, Any]) -> str:
    task_id = payload.get("task_id", "")
    title = payload.get("title", "")
    description = payload.get("description", "")
    task_context = payload.get("context", {})

    logger.info("Processing task_id=%s title=%r", task_id, title)

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

    roster_config = AGENT_ROSTER.get("ux_researcher", {})
    model = roster_config.get("model", "glm-4.7-flash")
    system_prompt = roster_config.get("system_prompt", "")

    user_prompt = f"Task Description:\n{description}\n\n"
    if memory_context:
        user_prompt += f"Relevant Memory Context:\n{memory_context}\n\n"
    if task_context:
        user_prompt += f"Task Context Metadata:\n{json.dumps(task_context, indent=2)}\n\n"
    user_prompt += (
        "Analyze user needs and synthesize findings into actionable recommendations. "
        "Write your deliverable to a file in the workspace directory."
    )

    output = ""
    try:
        output = "".join(list(ask_local_with_tools(
            user_input=user_prompt,
            model=model,
            system_extra=system_prompt,
            tools=["web_search", "read_file"],
        )))
    except Exception as exc:
        logger.error("LLM execution failed: %s", exc)
        output = f"Error: LLM execution failed: {exc}"

    event_bus_url = os.getenv("EVENT_BUS_URL", "http://localhost:8766").rstrip("/")
    if task_id:
        try:
            httpx.post(
                f"{event_bus_url}/results",
                json={"task_id": task_id, "output": output, "needs_review": False},
                headers={"X-Jarvis-Agent-ID": "ux_researcher"},
                timeout=5.0,
            )
        except Exception as exc:
            logger.warning("Could not post results to event bus: %s", exc)

    return output
