"""
Tests for core/manager.py — Jarvis Manager Core Loop.

All LLM, memory, security_reviewer, and HTTP calls are mocked.
Tests cover: decomposition, security gate logic, publish fallback, and
ExecutionPlan integrity.
"""
import json
import sys
import uuid
import pytest
from unittest.mock import MagicMock, patch, call

# ── Stubs before any import ────────────────────────────────────────────────────

_mock_config = MagicMock()
_mock_config.LOCAL_DEFAULT = "glm-4.7-flash"
_mock_config.AGENT_ROSTER  = {
    "researcher":      {"role": "Research agent.", "tools": ["web_search"], "model": "glm-4.7-flash"},
    "backend_engineer":{"role": "Backend engineer.", "tools": ["code", "shell"], "model": "glm-4.7-flash"},
    "devops_release":  {"role": "DevOps engineer.",  "tools": ["shell"],         "model": "glm-4.7-flash"},
    "security_reviewer":{"role": "Security engineer.", "tools": ["file_read"],   "model": "glm-4.7-flash"},
}
sys.modules["config"]             = _mock_config
sys.modules["brains.brain_ollama"] = MagicMock()
sys.modules["skills"]             = MagicMock()
sys.modules["tool_registry"]      = MagicMock()

for mod in list(sys.modules):
    if mod.startswith("core.manager") or mod == "core.manager":
        del sys.modules[mod]

import importlib
import core.manager as manager_mod
from core.manager import (
    JarvisManager, AgentTask, ExecutionPlan,
    _decompose_via_llm, _run_security_gate, _publish,
    _ALWAYS_REVIEW,
)

# Import the real security_reviewer types at module level — test_hackingtool_adapter.py
# stubs sys.modules["agents.security_reviewer"] during its own collection (which happens
# after this file). Capturing the real classes now ensures test functions get the
# dataclasses, not MagicMock auto-attributes, regardless of pytest collection order.
from agents.security_reviewer import SecurityVerdict, SecurityFinding


@pytest.fixture(autouse=True)
def _restore_config():
    """Re-assert this module's config stub before each test.

    test_rbac._make_registry() clobbers sys.modules["config"] at execution
    time with a stub that lacks 'role' keys. Re-asserting here ensures
    manager tests always see the correct roster.
    """
    sys.modules["config"] = _mock_config
    yield


def _good_llm_response(tasks: list[dict]) -> str:
    return json.dumps({"goal_summary": "test goal", "tasks": tasks})


def _make_task(agent: str = "researcher", needs_review: bool = False) -> AgentTask:
    return AgentTask(
        title="Test task",
        description="Do something useful",
        agent=agent,
        priority=5,
        needs_security_review=needs_review,
        task_id=str(uuid.uuid4()),
    )


# ── _decompose_via_llm ────────────────────────────────────────────────────────

def test_decompose_returns_agent_tasks():
    raw = _good_llm_response([
        {"title": "Research AI", "description": "Look up AI trends", "agent": "researcher",
         "priority": 7, "needs_security_review": False, "context": {}},
        {"title": "Build API",   "description": "Write FastAPI endpoint", "agent": "backend_engineer",
         "priority": 6, "needs_security_review": True, "context": {}},
    ])
    with patch.object(manager_mod, "_decompose_via_llm", wraps=_decompose_via_llm):
        with patch("brains.brain_ollama.ask_local_structured", return_value=raw):
            tasks = _decompose_via_llm("Build an AI-powered API", "")
    assert len(tasks) == 2
    assert tasks[0].agent == "researcher"
    assert tasks[1].agent == "backend_engineer"
    assert tasks[1].needs_security_review is True


def test_decompose_remaps_unknown_agent_to_researcher():
    raw = _good_llm_response([
        {"title": "Task", "description": "Do it", "agent": "nonexistent_agent",
         "priority": 5, "needs_security_review": False, "context": {}},
    ])
    with patch("brains.brain_ollama.ask_local_structured", return_value=raw):
        tasks = _decompose_via_llm("goal", "")
    assert tasks[0].agent == "researcher"


def test_decompose_falls_back_on_llm_error():
    with patch("brains.brain_ollama.ask_local_structured", side_effect=RuntimeError("timeout")):
        tasks = _decompose_via_llm("some goal", "")
    assert len(tasks) == 1
    assert tasks[0].agent == "researcher"


def test_decompose_strips_think_tags():
    raw = "<think>reasoning...</think>" + _good_llm_response([
        {"title": "T", "description": "D", "agent": "researcher",
         "priority": 5, "needs_security_review": False, "context": {}},
    ])
    with patch("brains.brain_ollama.ask_local_structured", return_value=raw):
        tasks = _decompose_via_llm("goal", "")
    assert len(tasks) == 1
    assert tasks[0].title == "T"


def test_decompose_falls_back_on_empty_tasks_array():
    raw = _good_llm_response([])  # empty tasks
    with patch("brains.brain_ollama.ask_local_structured", return_value=raw):
        tasks = _decompose_via_llm("goal", "")
    assert len(tasks) == 1   # fallback single task


# ── Security gate ─────────────────────────────────────────────────────────────

def test_security_gate_passes_clean_task():
    verdict = SecurityVerdict(verdict="PASS", severity="none", findings=[], summary="clean")
    task = _make_task("researcher", needs_review=True)
    with patch("agents.security_reviewer.review", return_value=verdict):
        approved = _run_security_gate(task)
    assert approved is True
    assert task.status != "security_blocked"
    assert task.security_verdict == "PASS"


def test_security_gate_blocks_on_fail_verdict():
    verdict = SecurityVerdict(
        verdict="FAIL", severity="critical",
        findings=[SecurityFinding(
            type="CREDENTIAL_EXPOSURE", severity="critical",
            location="description", description="Found API key",
            recommendation="Remove key.",
        )],
        summary="Credential detected.",
    )
    task = _make_task("backend_engineer", needs_review=True)
    with patch("agents.security_reviewer.review", return_value=verdict):
        approved = _run_security_gate(task)
    assert approved is False
    assert task.status == "security_blocked"
    assert task.security_verdict == "FAIL"


def test_security_gate_fails_closed_on_exception():
    task = _make_task("researcher", needs_review=True)
    with patch("agents.security_reviewer.review", side_effect=RuntimeError("service down")):
        approved = _run_security_gate(task)
    assert approved is False
    assert task.status == "security_blocked"
    assert task.security_verdict == "gate_error"


def test_devops_release_always_in_always_review_set():
    assert "devops_release" in _ALWAYS_REVIEW


# ── Publishing ────────────────────────────────────────────────────────────────

def test_publish_uses_event_bus_when_reachable():
    task = _make_task("researcher")
    mock_resp = MagicMock()
    mock_resp.status_code = 202
    mock_resp.json.return_value = {"task_id": "bus-task-1"}

    with patch("httpx.post", return_value=mock_resp) as mock_post:
        task_id = _publish(task)

    assert task_id == "bus-task-1"
    assert task.status == "scheduled"
    mock_post.assert_called_once()
    assert "jarvis_manager" in str(mock_post.call_args)


def test_publish_uses_event_bus_url_from_env(monkeypatch):
    task = _make_task("researcher")
    mock_resp = MagicMock()
    mock_resp.status_code = 202
    mock_resp.json.return_value = {"task_id": "bus-task-2"}
    monkeypatch.setenv("EVENT_BUS_URL", "http://event_bus:8766/")

    with patch("httpx.post", return_value=mock_resp) as mock_post:
        task_id = _publish(task)

    assert task_id == "bus-task-2"
    assert mock_post.call_args.args[0] == "http://event_bus:8766/tasks"


def test_publish_falls_back_to_direct_dispatch_when_bus_unreachable():
    import httpx
    task = _make_task("researcher")
    with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
        with patch("agent_dispatch.dispatch", return_value=iter(["result text"])):
            task_id = _publish(task)
    assert task_id is not None
    assert task.status == "done"
    assert task.context.get("_output") == "result text"


def test_publish_direct_marks_failed_on_dispatch_error():
    import httpx
    task = _make_task("researcher")
    with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
        with patch("agent_dispatch.dispatch", side_effect=RuntimeError("dispatch failed")):
            task_id = _publish(task)
    assert task.status == "failed"
    assert "_error" in task.context


# ── JarvisManager.run() ───────────────────────────────────────────────────────

def test_manager_run_returns_execution_plan():
    mgr = JarvisManager()
    raw = _good_llm_response([
        {"title": "Research", "description": "Look it up", "agent": "researcher",
         "priority": 5, "needs_security_review": False, "context": {}},
    ])
    mock_resp = MagicMock()
    mock_resp.status_code = 202
    mock_resp.json.return_value = {"task_id": "t-1"}

    with patch("brains.brain_ollama.ask_local_structured", return_value=raw):
        with patch("httpx.post", return_value=mock_resp):
            with patch.object(mgr, "_get_memory_context", return_value=""):
                with patch.object(mgr, "_record_to_memory"):
                    plan = mgr.run("Research quantum computing", session_id="s-1")

    assert isinstance(plan, ExecutionPlan)
    assert plan.goal == "Research quantum computing"
    assert len(plan.tasks) == 1
    assert plan.tasks[0].status == "scheduled"


def test_manager_run_blocks_security_failed_task():
    mgr = JarvisManager()
    raw = _good_llm_response([
        {"title": "Deploy to prod", "description": "rm -rf /tmp && deploy",
         "agent": "devops_release",   # always reviewed
         "priority": 8, "needs_security_review": True, "context": {}},
    ])
    fail_verdict = SecurityVerdict(
        verdict="FAIL", severity="critical",
        findings=[], summary="Destructive command detected.",
    )

    with patch("brains.brain_ollama.ask_local_structured", return_value=raw):
        with patch("agents.security_reviewer.review", return_value=fail_verdict):
            with patch.object(mgr, "_get_memory_context", return_value=""):
                with patch.object(mgr, "_record_to_memory"):
                    plan = mgr.run("Deploy everything", session_id="s-2")

    assert plan.blocked_count == 1
    assert plan.tasks[0].status == "security_blocked"
    assert plan.tasks[0].security_verdict == "FAIL"


def test_manager_run_skips_security_gate_for_low_risk_agent():
    mgr = JarvisManager()
    raw = _good_llm_response([
        {"title": "Research topic", "description": "Search the web",
         "agent": "researcher",
         "priority": 5, "needs_security_review": False, "context": {}},
    ])
    mock_resp = MagicMock()
    mock_resp.status_code = 202
    mock_resp.json.return_value = {"task_id": "t-2"}

    with patch("brains.brain_ollama.ask_local_structured", return_value=raw):
        with patch("httpx.post", return_value=mock_resp):
            with patch.object(mgr, "_get_memory_context", return_value=""):
                with patch.object(mgr, "_record_to_memory"):
                    with patch("agents.security_reviewer.review") as mock_review:
                        plan = mgr.run("Research AI trends", session_id="s-3")

    # Security gate must NOT be called for researcher with needs_security_review=False
    mock_review.assert_not_called()
    assert plan.tasks[0].status == "scheduled"


def test_manager_run_assigns_session_id_when_missing():
    mgr = JarvisManager()
    raw = _good_llm_response([
        {"title": "T", "description": "D", "agent": "researcher",
         "priority": 5, "needs_security_review": False, "context": {}},
    ])
    mock_resp = MagicMock()
    mock_resp.status_code = 202
    mock_resp.json.return_value = {"task_id": "t-auto"}

    with patch("brains.brain_ollama.ask_local_structured", return_value=raw):
        with patch("httpx.post", return_value=mock_resp):
            with patch.object(mgr, "_get_memory_context", return_value=""):
                with patch.object(mgr, "_record_to_memory"):
                    plan = mgr.run("Do something")

    assert plan.session_id  # auto-generated UUID, not empty


def test_execution_plan_to_dict():
    plan = ExecutionPlan(
        goal="Test goal", session_id="s", project_id="p",
        tasks=[AgentTask(title="T", description="D", agent="researcher",
                         task_id="task-1", status="done")],
    )
    d = plan.to_dict()
    assert d["goal"] == "Test goal"
    assert d["tasks"][0]["task_id"] == "task-1"
    assert d["tasks"][0]["status"] == "done"
