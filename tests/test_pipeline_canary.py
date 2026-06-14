"""End-to-end manager pipeline canary.

This exercises the real /manager/run-stream path while keeping execution local,
fast, and side-effect free.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from unittest.mock import patch

from fastapi.testclient import TestClient


def _parse_sse_events(body: str) -> list[dict]:
    events: list[dict] = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if not line.startswith("data: "):
                continue
            raw = line[len("data: ") :].strip()
            if not raw or raw == "[DONE]":
                continue
            events.append(json.loads(raw))
    return events


def _agent_tasks():
    from core.manager import AgentTask

    return [
        AgentTask(
            title="Backend route audit",
            description="Inspect the local manager API route contract and report one low-risk improvement.",
            agent="backend_engineer",
            priority=10,
        ),
        AgentTask(
            title="Dashboard state audit",
            description="Review the local dashboard state layout and report one usability improvement.",
            agent="frontend_designer",
            priority=9,
        ),
        AgentTask(
            title="Interaction friction audit",
            description="Assess the task-review flow and report one human-in-the-loop friction point.",
            agent="ux_researcher",
            priority=8,
        ),
        AgentTask(
            title="Security gate audit",
            description="Review the pipeline canary plan for local-only execution and non-sensitive test data.",
            agent="security_reviewer",
            priority=7,
        ),
        AgentTask(
            title="Regression checklist audit",
            description="Define a narrow smoke checklist for the manager stream and dashboard-visible task state.",
            agent="qa_tester",
            priority=6,
        ),
        AgentTask(
            title="Research digest audit",
            description="Summarize local-first context optimization ideas using only provided project context.",
            agent="researcher",
            priority=5,
        ),
        AgentTask(
            title="Packaging readiness audit",
            description="Prepare a local packaging-readiness checklist with no file changes and no external state.",
            agent="devops_release",
            priority=4,
        ),
        AgentTask(
            title="Memory retrieval audit",
            description="Suggest a local memory recall checkpoint for the manager project loop.",
            agent="memory_librarian",
            priority=3,
        ),
    ]


def _fake_dispatch(agent: str, task: str, context: str = "") -> Iterable[str]:
    if agent in {"backend_engineer", "frontend_designer", "qa_tester", "automation_engineer"}:
        return iter(
            [
                "```text\n",
                f"{agent} completed the scoped canary task.\n",
                f"Task: {task}\n",
                "Verification: manager stream emitted a concrete artifact for dashboard review.\n",
                "```\n",
                "Debrief: no external calls, no file writes, no sensitive data, rollback not required.",
            ]
        )
    return iter(
        [
            f"{agent} completed the scoped canary task. ",
            f"Task: {task}. ",
            "Verification: manager stream produced reviewable output. ",
            "Debrief: no external calls, no file writes, no sensitive data, rollback not required.",
        ]
    )


def test_manager_stream_assigns_each_core_agent_and_records_dashboard_tasks(monkeypatch):
    import api
    import task_runtime
    import task_persistence
    from core.manager import manager

    persisted: dict[str, object] = {"tasks": [], "events": {}}
    monkeypatch.setattr(api, "_API_TOKEN", "")
    monkeypatch.setattr(api, "_CLOUD_FIRST_AGENTS", set())
    monkeypatch.setattr(api, "_append_eval_log", lambda entry: None)
    monkeypatch.setattr(task_persistence, "load_snapshot", lambda: persisted)
    monkeypatch.setattr(task_persistence, "reset_for_tests", lambda: None)
    monkeypatch.setattr(task_persistence, "upsert_task", lambda task: None)
    monkeypatch.setattr(task_persistence, "append_event", lambda event, index: None)
    monkeypatch.setattr(
        task_runtime,
        "_task_requires_approval",
        lambda *args, **kwargs: (False, "", {"score": 0.98, "source": "canary"}),
    )
    monkeypatch.setattr(task_runtime, "_start_task_thread", lambda task_id: None)
    monkeypatch.setattr(
        task_runtime.worktree_manager,
        "prepare_isolated_workspace",
        lambda task_id, prompt, *, enabled=True: {
            "ok": True,
            "enabled": False,
            "created": False,
            "reason": "disabled_in_canary",
        },
    )

    task_runtime.reset_for_tests()
    client = TestClient(api.app)
    with (
        patch.object(manager, "decompose", return_value=_agent_tasks()),
        patch("agent_dispatch.dispatch", side_effect=_fake_dispatch),
        patch(
            "api._run_eval",
            return_value={
                "verdict": "pass",
                "score": 93,
                "feedback": "Canary output is concrete and reviewable.",
                "issues": [],
            },
        ),
    ):
        with client.stream(
            "POST",
            "/manager/run-stream",
            json={"goal": "Canary the Jarvis manager pipeline", "cloud_plan": False},
        ) as response:
            assert response.status_code == 200
            events = _parse_sse_events("".join(response.iter_text()))

    plan_events = [event for event in events if event.get("type") == "plan"]
    assert len(plan_events) == 1
    planned_agents = {task["agent"] for task in plan_events[0]["tasks"]}
    assert planned_agents == {
        "backend_engineer",
        "frontend_designer",
        "ux_researcher",
        "security_reviewer",
        "qa_tester",
        "researcher",
        "devops_release",
        "memory_librarian",
    }

    starts = [event for event in events if event.get("type") == "start"]
    evals = [event for event in events if event.get("type") == "eval"]
    dones = [event for event in events if event.get("type") == "done"]
    blocked = [event for event in events if event.get("type") == "blocked"]
    assert len(starts) == 8
    assert len(evals) == 8
    assert len(dones) == 8
    assert not blocked
    assert events[-1]["type"] == "complete"

    tasks = task_runtime.list_tasks(limit=20)
    canary_tasks = [
        task for task in tasks
        if task.get("source") == "manager"
        and str(task.get("assigned_agent_id", "")).replace("-", "_") in planned_agents
    ]
    assert len(canary_tasks) == 8
    assert {task["status"] for task in canary_tasks} == {"succeeded"}
    assert all("Debrief:" in task.get("result", "") for task in canary_tasks)

    health = api._pipeline_health_check()
    assert health["ok"] is True
    assert health["by_status"].get("succeeded") == 8
    task_runtime.reset_for_tests()
