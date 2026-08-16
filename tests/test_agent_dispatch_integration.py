"""
Integration tests for the Jarvis agent dispatch pipeline.

Covers:
  - task_runtime: submit → dispatch → complete (backend-engineer, researcher kind)
  - task_runtime: approval gate (waiting_approval stays gated until approve_task())
  - task_runtime: approve_task() then completes
  - core/manager: decompose goal → AgentTask list via mocked LLM
  - agent_dispatch.dispatch(): routes to Ollama via mocked ask_local_with_tools
  - agent_worker.run_once(): processes event bus payload via mocked process_task

Notes on implementation:
  - There is no agents/researcher.py — only researcher.md and ux_researcher.py.
    task_runtime routes kind="research" to the "researcher" agent-id but processes
    it through smart_stream (not a separate process_task module).
  - agent_dispatch.dispatch() routes to Ollama (ask_local_with_tools), not to
    agents/*.process_task(); the two dispatch paths are separate.
  - task_runtime must be imported at module level (before any PyQt6 sub-module
    stub is applied) because router.py → desktop/overlay.py pulls PyQt6.QtCore
    at import time.
"""
from __future__ import annotations

import json
import sys
import pytest
from unittest.mock import MagicMock, patch

# task_runtime (and transitively router → desktop/overlay) must be imported at
# module-collection time, before per-test PyQt6 sub-module stubs are injected.
import task_runtime
from agents import agent_worker


def _fake_workspace() -> dict:
    return {
        "ok": False, "enabled": False, "created": False,
        "reason": "", "repo_root": "", "worktree_path": "", "branch": "",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Test 1 — submit kind="code" → backend-engineer → succeeded
# ─────────────────────────────────────────────────────────────────────────────

def test_submit_task_queues_and_dispatches_to_backend_engineer():
    # Arrange
    task_runtime.reset_for_tests()

    with patch("task_runtime.smart_stream", return_value=(iter(["done"]), "MockModel")), \
         patch("task_runtime.worktree_manager.prepare_isolated_workspace", return_value=_fake_workspace()):
        # Act
        task = task_runtime.submit_task(
            "Write a pytest fixture for the auth module", kind="code"
        )
        result = task_runtime.wait_for_task(task["id"], timeout=5.0)

    # Assert
    assert result is not None
    assert result["assigned_agent_id"] == "backend-engineer"
    assert result["status"] == "succeeded"


# ─────────────────────────────────────────────────────────────────────────────
# Test 2 — submit kind="research" → researcher agent-id → succeeded
# ─────────────────────────────────────────────────────────────────────────────

def test_submit_task_queues_and_dispatches_to_researcher():
    # Arrange
    task_runtime.reset_for_tests()

    with patch("task_runtime.smart_stream", return_value=(iter(["research result"]), "MockModel")), \
         patch("task_runtime.worktree_manager.prepare_isolated_workspace", return_value=_fake_workspace()):
        # Act
        task = task_runtime.submit_task(
            "Research best practices for vector database indexing",
            kind="research",
        )
        result = task_runtime.wait_for_task(task["id"], timeout=5.0)

    # Assert
    assert result is not None
    assert result["assigned_agent_id"] == "researcher"
    assert result["status"] == "succeeded"


# ─────────────────────────────────────────────────────────────────────────────
# Test 3 — approval gate: "git push" prompt stays in waiting_approval
# ─────────────────────────────────────────────────────────────────────────────

def test_task_requiring_approval_stays_in_waiting_approval():
    # Arrange
    task_runtime.reset_for_tests()

    with patch("task_runtime.smart_stream") as mock_route, \
         patch("task_runtime.worktree_manager.prepare_isolated_workspace", return_value=_fake_workspace()):
        # Act
        task = task_runtime.submit_task("git push origin main", kind="code")

    # Assert — gate is holding, LLM never called
    fetched = task_runtime.get_task(task["id"])
    assert fetched is not None
    assert fetched["status"] == "waiting_approval"
    mock_route.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Test 4 — approve_task() lifts the gate and task completes
# ─────────────────────────────────────────────────────────────────────────────

def test_approve_task_then_dispatches():
    # Arrange
    task_runtime.reset_for_tests()

    with patch("task_runtime.worktree_manager.prepare_isolated_workspace", return_value=_fake_workspace()):
        task = task_runtime.submit_task("deploy the staging environment", kind="code")

    assert task_runtime.get_task(task["id"])["status"] == "waiting_approval"

    # Act
    with patch("task_runtime.smart_stream", return_value=(iter(["deployed"]), "MockModel")):
        task_runtime.approve_task(task["id"])
        result = task_runtime.wait_for_task(task["id"], timeout=5.0)

    # Assert
    assert result is not None
    assert result["status"] == "succeeded"
    assert result["autonomy"] == "human_approved"


# ─────────────────────────────────────────────────────────────────────────────
# Test 5 — core.manager decomposes a goal into AgentTask list via mocked LLM
# ─────────────────────────────────────────────────────────────────────────────

def test_manager_decomposes_goal_into_subtasks():
    # Arrange: stub config so the roster is controlled, then reload core.manager
    original_manager = sys.modules.get("core.manager")
    _mock_config = MagicMock()
    _mock_config.LOCAL_DEFAULT = "glm-4.7-flash"
    _mock_config.AGENT_ROSTER = {
        "researcher":       {"role": "Research.", "tools": ["web_search"], "model": "glm-4.7-flash"},
        "backend_engineer": {"role": "Backend.", "tools": ["code"],       "model": "glm-4.7-flash"},
    }

    try:
        with patch.dict(sys.modules, {
            "config": _mock_config,
            "brains.brain_ollama": MagicMock(),
            "infra.memory": MagicMock(),
        }):
            for mod in list(sys.modules):
                if "core.manager" in mod:
                    del sys.modules[mod]

            from core.manager import _decompose_via_llm

            llm_response = json.dumps({
                "goal_summary": "Build a research-backed API",
                "tasks": [
                    {"title": "Research AI APIs", "description": "Find top AI API patterns",
                     "agent": "researcher", "priority": 6, "needs_security_review": False, "context": {}},
                    {"title": "Implement endpoint", "description": "Write FastAPI endpoint",
                     "agent": "backend_engineer", "priority": 7, "needs_security_review": False, "context": {}},
                ],
            })

            # Act
            with patch("core.manager.ask_local_structured", return_value=llm_response):
                tasks = _decompose_via_llm("Build a research-backed AI API", memory_ctx="")
    finally:
        if original_manager is None:
            sys.modules.pop("core.manager", None)
        else:
            sys.modules["core.manager"] = original_manager

    # Assert
    assert len(tasks) == 2
    agents = {t.agent for t in tasks}
    assert "researcher" in agents
    assert "backend_engineer" in agents


# ─────────────────────────────────────────────────────────────────────────────
# Test 6 — agent_dispatch.dispatch() routes to correct Ollama call
# ─────────────────────────────────────────────────────────────────────────────

def test_agent_dispatch_routes_to_ollama_for_backend_engineer():
    """
    agent_dispatch.dispatch() is the Ollama-backed path (separate from task_runtime).
    Verify it routes to ask_local_with_tools with the correct model from AGENT_ROSTER.
    """
    # Arrange
    _mock_config = MagicMock()
    _mock_config.AGENT_ROSTER = {
        "backend_engineer": {
            "role": "Backend engineer.",
            "tools": ["code"],
            "model": "glm-4.7-flash",
            "system_prompt": "You are the backend engineer.",
        },
    }
    mock_brain = MagicMock()
    mock_brain.ask_local_with_tools = MagicMock(return_value=iter(["patch applied"]))

    with patch.dict(sys.modules, {
        "config": _mock_config,
        "brains.brain_ollama": mock_brain,
    }):
        for mod in list(sys.modules):
            if mod == "agent_dispatch":
                del sys.modules[mod]

        import agent_dispatch

        # Act
        chunks = list(agent_dispatch.dispatch("backend_engineer", "Fix the login bug"))

    # Assert
    assert chunks == ["patch applied"]
    mock_brain.ask_local_with_tools.assert_called_once()
    assert mock_brain.ask_local_with_tools.call_args.kwargs.get("model") == "glm-4.7-flash"


# ─────────────────────────────────────────────────────────────────────────────
# Test 7 — agent_worker.run_once() processes an event bus task via process_task
# ─────────────────────────────────────────────────────────────────────────────

def test_agent_worker_run_once_processes_task_via_process_task():
    """
    agent_worker.run_once() polls the event bus and dispatches to process_task().
    Mock both the HTTP inbox response and process_task to verify the wiring.
    """
    # Arrange
    sse_body = (
        'data: {"type": "task", "task_id": "t-123", '
        '"task": {"title": "Fix auth", "description": "Fix the login middleware", "context": {}}}\n'
    )
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.text = sse_body

    mock_process = MagicMock(return_value="task completed successfully")

    with patch("httpx.get", return_value=mock_response), \
         patch.object(agent_worker, "_load_process_task", return_value=mock_process):
        # Act
        result = agent_worker.run_once("backend_engineer", timeout_ms=100)

    # Assert
    assert result["ok"] is True
    assert result["status"] == "processed"
    assert result["task_id"] == "t-123"
    mock_process.assert_called_once()
    payload = mock_process.call_args[0][0]
    assert payload["title"] == "Fix auth"


# ─────────────────────────────────────────────────────────────────────────────
# Test 8 — core.work_order.preview_work_order() parses a JSON brief
# ─────────────────────────────────────────────────────────────────────────────

def test_work_order_preview_parses_json_brief():
    """
    preview_work_order() must return a dict with keys: tasks, task_count,
    review_required_count, execute=False, and a note string.
    Each task dict must have at minimum: title, agent, review_required.
    """
    brief = json.dumps({
        "tasks": [
            {"title": "Research caching strategies", "description": "Survey Redis vs Memcached",
             "agent": "researcher", "priority": 5, "needs_security_review": False, "context": {}},
            {"title": "Implement caching layer", "description": "Add Redis cache to API",
             "agent": "backend_engineer", "priority": 7, "needs_security_review": False, "context": {}},
        ]
    })

    from core.work_order import preview_work_order

    result = preview_work_order(brief, source="test")

    assert result["execute"] is False
    assert isinstance(result["tasks"], list)
    assert result["task_count"] == 2
    assert isinstance(result["review_required_count"], int)
    agents = {t["agent"] for t in result["tasks"]}
    assert "researcher" in agents
    assert "backend_engineer" in agents
    for t in result["tasks"]:
        assert "title" in t
        assert "review_required" in t
