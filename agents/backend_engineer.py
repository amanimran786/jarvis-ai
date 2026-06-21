"""
agents/backend_engineer.py
Worker module for the Backend Engineer agent.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from brains.brain_ollama import ask_local_with_tools
from config import AGENT_ROSTER

logger = logging.getLogger("jarvis.agent.backend_engineer")

# Fallback imports to ensure graceful degradation if dependencies are missing or services are down
try:
    from infra.memory import store
except ImportError:
    store = None
    logger.warning("infra.memory.store could not be imported. Vector memory is disabled.")


def process_task(payload: dict[str, Any]) -> str:
    """
    Process a backend engineering task.

    1. Consumes a task payload (containing task_id, title, description, and context).
    2. Recalls relevant context from infra.memory.store based on title and description.
    3. Sets up workspace confinement environment context.
    4. Invokes the local LLM using the ask_local_with_tools agentic loop.
    5. Outputs the results back to the event bus.
    """
    task_id = payload.get("task_id", "")
    title = payload.get("title", "")
    description = payload.get("description", "")
    task_context = payload.get("context", {})

    logger.info("Processing task_id=%s title=%r", task_id, title)

    # 1. Recall relevant memory context
    memory_context = ""
    if store:
        try:
            query = f"{title} {description}".strip()
            # Look up context relative to session and project if provided
            session_id = task_context.get("session_id")
            project_id = task_context.get("project_id")
            memory_context = store.context_for_prompt(
                query=query,
                top_k=3,
                session_id=session_id or None,
                project_id=project_id or None,
                max_chars=1500,
            )
        except Exception as exc:
            logger.warning("Memory context recall failed: %s", exc)

    # 2. Retrieve agent configuration from config.py
    roster_config = AGENT_ROSTER.get("backend_engineer", {})
    model = roster_config.get("model", "glm-4.7-flash")
    system_prompt = roster_config.get("system_prompt", "")

    # 3. Format prompt for the model
    user_prompt = f"Task Description:\n{description}\n\n"
    if memory_context:
        user_prompt += f"Relevant Memory Context:\n{memory_context}\n\n"
    if task_context:
        user_prompt += f"Task Context Metadata:\n{json.dumps(task_context, indent=2)}\n\n"

    user_prompt += "Please resolve the task inside the workspace directory using your tools."

    # 4. Invoke local LLM with tool routing
    output = ""
    try:
        # We explicitly request 'read_file', 'write_file', and 'run_tests' tools
        response_generator = ask_local_with_tools(
            user_input=user_prompt,
            model=model,
            system_extra=system_prompt,
            tools=["read_file", "write_file", "run_tests"],
            workspace_confined=True,
        )
        output = "".join(list(response_generator))
    except Exception as exc:
        logger.error("LLM tool-calling loop execution failed: %s", exc)
        output = f"Error: LLM execution failed: {exc}"
    # 5. Output results back to the event bus /results endpoint
    event_bus_url = os.getenv("EVENT_BUS_URL", "http://localhost:8766").rstrip("/")
    if task_id:
        try:
            result_payload = {
                "task_id": task_id,
                "output": output,
                "needs_review": False,  # Default to false, can be set based on criteria
            }
            logger.info("Posting results for task_id=%s to event bus...", task_id)
            resp = httpx.post(
                f"{event_bus_url}/results",
                json=result_payload,
                headers={"X-Jarvis-Agent-ID": "backend_engineer"},
                timeout=5.0,
            )
            if resp.status_code == 200 or resp.status_code == 202:
                logger.info("Successfully posted task results to event bus.")
            else:
                logger.warning(
                    "Event bus returned status code %d: %s",
                    resp.status_code,
                    resp.text,
                )
        except Exception as exc:
            logger.warning(
                "Could not publish results back to event bus (service down or network timeout): %s",
                exc,
            )

    return output
