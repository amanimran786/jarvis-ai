"""
Minimal backend agent worker.

Polls the Redis-backed event bus inbox over HTTP and hands one task at a time
to agents.backend_engineer.process_task(). This keeps the first manager ->
event bus -> worker -> result lane small and verifiable.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from typing import Any

import httpx

from agents.backend_engineer import process_task

log = logging.getLogger("jarvis.agent.backend_worker")


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
            log.warning("Skipping malformed worker event: %r", payload[:120])
    return events


def _payload_from_event(event: dict[str, Any]) -> dict[str, Any] | None:
    if event.get("type") != "task":
        return None
    task = event.get("task")
    if not isinstance(task, dict):
        return None
    return {
        "task_id": event.get("task_id", ""),
        "title": task.get("title", "Backend task"),
        "description": task.get("description", ""),
        "context": task.get("context") if isinstance(task.get("context"), dict) else {},
    }


def run_once(agent_name: str = "backend_engineer", timeout_ms: int = 5000) -> dict[str, Any]:
    """
    Poll one inbox request and process the first task event, if any.
    """
    if agent_name != "backend_engineer":
        return {"ok": False, "status": "unsupported_agent", "agent": agent_name}

    try:
        response = httpx.get(
            f"{_event_bus_url()}/agent/{agent_name}/inbox",
            params={"timeout_ms": timeout_ms},
            timeout=max(5.0, (timeout_ms / 1000.0) + 2.0),
        )
        response.raise_for_status()
    except Exception as exc:
        log.warning("Worker inbox poll failed: %s", exc)
        return {"ok": False, "status": "poll_failed", "error": str(exc)}

    for event in _parse_sse_events(response.text):
        payload = _payload_from_event(event)
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
    parser = argparse.ArgumentParser(description="Run a Jarvis backend agent worker.")
    parser.add_argument("--agent", default="backend_engineer")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--timeout-ms", type=int, default=5000)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.once:
        print(json.dumps(run_once(agent_name=args.agent, timeout_ms=args.timeout_ms)))
        return
    run_forever(agent_name=args.agent, timeout_ms=args.timeout_ms)


if __name__ == "__main__":
    main()
