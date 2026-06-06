"""
agents/agent_worker.py

Generic agent worker — polls an event bus inbox and dispatches to the
appropriate process_task() implementation.

Supported agents:
  backend_engineer, frontend_designer, ux_researcher,
  qa_tester, devops_release, memory_librarian

Usage:
  python -m agents.agent_worker --agent frontend_designer
  python -m agents.agent_worker --agent devops_release --once
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import time
from typing import Any

import httpx

log = logging.getLogger("jarvis.agent_worker")

_SUPPORTED_AGENTS = {
    "backend_engineer",
    "frontend_designer",
    "ux_researcher",
    "qa_tester",
    "devops_release",
    "memory_librarian",
}


def _event_bus_url() -> str:
    return os.getenv("EVENT_BUS_URL", "http://localhost:8766").rstrip("/")


def _parse_sse_events(body: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            log.warning("Skipping malformed event: %r", payload[:120])
    return events


def _payload_from_event(event: dict[str, Any], agent_name: str) -> dict[str, Any] | None:
    if event.get("type") != "task":
        return None
    task = event.get("task")
    if not isinstance(task, dict):
        return None
    return {
        "task_id": event.get("task_id", ""),
        "title": task.get("title", agent_name + " task"),
        "description": task.get("description", ""),
        "context": task.get("context") if isinstance(task.get("context"), dict) else {},
    }


def _load_process_task(agent_name: str):
    module = importlib.import_module(f"agents.{agent_name}")
    return module.process_task


def run_once(agent_name: str = "backend_engineer", timeout_ms: int = 5000) -> dict[str, Any]:
    if agent_name not in _SUPPORTED_AGENTS:
        return {"ok": False, "status": "unsupported_agent", "agent": agent_name}

    try:
        response = httpx.get(
            f"{_event_bus_url()}/agent/{agent_name}/inbox",
            params={"timeout_ms": timeout_ms},
            timeout=max(5.0, (timeout_ms / 1000.0) + 2.0),
        )
        response.raise_for_status()
    except Exception as exc:
        log.warning("Inbox poll failed for %s: %s", agent_name, exc)
        return {"ok": False, "status": "poll_failed", "error": str(exc)}

    process_task = _load_process_task(agent_name)

    for event in _parse_sse_events(response.text):
        payload = _payload_from_event(event, agent_name)
        if payload is None:
            continue
        output = process_task(payload)
        return {
            "ok": True,
            "status": "processed",
            "agent": agent_name,
            "task_id": payload.get("task_id", ""),
            "output_excerpt": output[:500],
        }

    return {"ok": True, "status": "idle", "agent": agent_name}


def run_forever(agent_name: str = "backend_engineer", timeout_ms: int = 5000, idle_sleep: float = 1.0) -> None:
    while True:
        result = run_once(agent_name=agent_name, timeout_ms=timeout_ms)
        if result.get("status") in {"idle", "poll_failed"}:
            time.sleep(idle_sleep)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Jarvis agent worker.")
    parser.add_argument("--agent", default="backend_engineer", choices=sorted(_SUPPORTED_AGENTS))
    parser.add_argument("--once", action="store_true", help="Process one task then exit.")
    parser.add_argument("--timeout-ms", type=int, default=5000)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.once:
        print(json.dumps(run_once(agent_name=args.agent, timeout_ms=args.timeout_ms)))
        return
    run_forever(agent_name=args.agent, timeout_ms=args.timeout_ms)


if __name__ == "__main__":
    main()
