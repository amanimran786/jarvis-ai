"""
Tests for agents/frontend_designer.py, ux_researcher.py, qa_tester.py,
devops_release.py, and agents/agent_worker.py.

All LLM, memory, and HTTP calls are mocked.
"""
from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

# ── Stubs before any project import ───────────────────────────────────────────

_mock_config = MagicMock()
_mock_config.LOCAL_DEFAULT = "glm-4.7-flash"
_mock_config.AGENT_ROSTER = {
    "frontend_designer": {
        "role": "Frontend engineer.",
        "tools": ["code", "file_read", "file_write"],
        "model": "glm-4.7-flash",
        "system_prompt": "FE Prompt",
    },
    "ux_researcher": {
        "role": "UX researcher.",
        "tools": ["web_search", "file_read"],
        "model": "glm-4.7-flash",
        "system_prompt": "UX Prompt",
    },
    "qa_tester": {
        "role": "QA engineer.",
        "tools": ["code", "shell", "file_read"],
        "model": "glm-4.7-flash",
        "system_prompt": "QA Prompt",
    },
    "devops_release": {
        "role": "DevOps engineer.",
        "tools": ["shell", "file_read", "file_write"],
        "model": "glm-4.7-flash",
        "system_prompt": "DevOps Prompt",
    },
}
sys.modules["config"] = _mock_config
sys.modules["brains.brain_ollama"] = MagicMock()

for _mod in [
    "infra.memory",
    "agents.frontend_designer",
    "agents.ux_researcher",
    "agents.qa_tester",
    "agents.devops_release",
    "agents.agent_worker",
]:
    sys.modules.pop(_mod, None)

import pytest
from unittest.mock import MagicMock


@pytest.fixture(autouse=True)
def _restore_stubs():
    sys.modules["config"] = _mock_config
    yield


def _make_payload(agent: str, task_id: str = "task-1") -> dict:
    return {
        "task_id": task_id,
        "title": f"{agent} task",
        "description": f"Do the {agent} work.",
        "context": {"session_id": "s1", "project_id": "p1"},
    }


# ── Helpers shared across agent tests ────────────────────────────────────────

def _run_agent(module_name: str, agent_key: str, tools: list[str]):
    """Import process_task from module_name and verify the standard flow."""
    sys.modules.pop(f"agents.{module_name}", None)
    mod = __import__(f"agents.{module_name}", fromlist=["process_task"])
    process_task = mod.process_task

    mock_store = MagicMock()
    mock_store.context_for_prompt.return_value = f"{agent_key} context"
    mock_brain = sys.modules["brains.brain_ollama"]
    mock_brain.ask_local_with_tools.reset_mock()
    mock_brain.ask_local_with_tools.return_value = iter(["output chunk"])

    with patch.object(mod, "store", mock_store), \
         patch("httpx.post") as mock_post:
        mock_post.return_value.status_code = 202
        result = process_task(_make_payload(agent_key))

    assert result == "output chunk"
    mock_store.context_for_prompt.assert_called_once()
    mock_brain.ask_local_with_tools.assert_called_once()
    assert mock_brain.ask_local_with_tools.call_args.kwargs["tools"] == tools
    mock_post.assert_called_once()
    posted = mock_post.call_args[1]["json"]
    assert posted["task_id"] == "task-1"
    assert posted["output"] == "output chunk"
    return result


# ── frontend_designer ─────────────────────────────────────────────────────────

def test_frontend_designer_process_task():
    _run_agent("frontend_designer", "frontend_designer", ["read_file", "write_file"])


def test_frontend_designer_posts_agent_id_header():
    sys.modules.pop("agents.frontend_designer", None)
    from agents.frontend_designer import process_task
    import agents.frontend_designer as mod
    mock_store = MagicMock()
    mock_store.context_for_prompt.return_value = ""
    sys.modules["brains.brain_ollama"].ask_local_with_tools.return_value = iter(["x"])
    with patch.object(mod, "store", mock_store), patch("httpx.post") as mock_post:
        mock_post.return_value.status_code = 202
        process_task(_make_payload("frontend_designer"))
    assert mock_post.call_args[1]["headers"]["X-Jarvis-Agent-ID"] == "frontend_designer"


def test_frontend_designer_survives_missing_store():
    sys.modules.pop("agents.frontend_designer", None)
    from agents.frontend_designer import process_task
    import agents.frontend_designer as mod
    sys.modules["brains.brain_ollama"].ask_local_with_tools.return_value = iter(["ok"])
    with patch.object(mod, "store", None), patch("httpx.post") as mock_post:
        mock_post.return_value.status_code = 202
        result = process_task(_make_payload("frontend_designer"))
    assert result == "ok"


# ── ux_researcher ─────────────────────────────────────────────────────────────

def test_ux_researcher_process_task():
    _run_agent("ux_researcher", "ux_researcher", ["web_search", "read_file"])


def test_ux_researcher_posts_agent_id_header():
    sys.modules.pop("agents.ux_researcher", None)
    from agents.ux_researcher import process_task
    import agents.ux_researcher as mod
    mock_store = MagicMock()
    mock_store.context_for_prompt.return_value = ""
    sys.modules["brains.brain_ollama"].ask_local_with_tools.return_value = iter(["rec"])
    with patch.object(mod, "store", mock_store), patch("httpx.post") as mock_post:
        mock_post.return_value.status_code = 202
        process_task(_make_payload("ux_researcher"))
    assert mock_post.call_args[1]["headers"]["X-Jarvis-Agent-ID"] == "ux_researcher"


# ── qa_tester ─────────────────────────────────────────────────────────────────

def test_qa_tester_process_task():
    _run_agent("qa_tester", "qa_tester", ["read_file", "write_file", "run_tests"])


def test_qa_tester_posts_agent_id_header():
    sys.modules.pop("agents.qa_tester", None)
    from agents.qa_tester import process_task
    import agents.qa_tester as mod
    mock_store = MagicMock()
    mock_store.context_for_prompt.return_value = ""
    sys.modules["brains.brain_ollama"].ask_local_with_tools.return_value = iter(["tests pass"])
    with patch.object(mod, "store", mock_store), patch("httpx.post") as mock_post:
        mock_post.return_value.status_code = 202
        process_task(_make_payload("qa_tester"))
    assert mock_post.call_args[1]["headers"]["X-Jarvis-Agent-ID"] == "qa_tester"


# ── devops_release ────────────────────────────────────────────────────────────

def test_devops_release_process_task():
    _run_agent("devops_release", "devops_release", ["read_file", "write_file", "run_tests"])


def test_devops_release_sets_needs_review_true():
    """devops_release always sets needs_review=True in its result post."""
    sys.modules.pop("agents.devops_release", None)
    from agents.devops_release import process_task
    import agents.devops_release as mod
    mock_store = MagicMock()
    mock_store.context_for_prompt.return_value = ""
    sys.modules["brains.brain_ollama"].ask_local_with_tools.return_value = iter(["deployed"])
    with patch.object(mod, "store", mock_store), patch("httpx.post") as mock_post:
        mock_post.return_value.status_code = 202
        process_task(_make_payload("devops_release"))
    assert mock_post.call_args[1]["json"]["needs_review"] is True


def test_devops_release_posts_agent_id_header():
    sys.modules.pop("agents.devops_release", None)
    from agents.devops_release import process_task
    import agents.devops_release as mod
    mock_store = MagicMock()
    mock_store.context_for_prompt.return_value = ""
    sys.modules["brains.brain_ollama"].ask_local_with_tools.return_value = iter(["done"])
    with patch.object(mod, "store", mock_store), patch("httpx.post") as mock_post:
        mock_post.return_value.status_code = 202
        process_task(_make_payload("devops_release"))
    assert mock_post.call_args[1]["headers"]["X-Jarvis-Agent-ID"] == "devops_release"


# ── agent_worker (generic) ────────────────────────────────────────────────────

def test_agent_worker_dispatches_to_frontend_designer():
    sys.modules.pop("agents.agent_worker", None)
    from agents import agent_worker

    event = (
        'data: {"type":"task","task_id":"t-fe","task":'
        '{"title":"Build nav","description":"Add nav bar","context":{"project_id":"p1"}}}\n\n'
    )
    response = MagicMock()
    response.text = event
    response.raise_for_status.return_value = None

    sys.modules.pop("agents.frontend_designer", None)
    with patch("httpx.get", return_value=response), \
         patch("agents.agent_worker._load_process_task") as mock_load:
        mock_pt = MagicMock(return_value="nav done")
        mock_load.return_value = mock_pt
        result = agent_worker.run_once("frontend_designer")

    assert result["ok"] is True
    assert result["status"] == "processed"
    assert result["task_id"] == "t-fe"
    mock_load.assert_called_once_with("frontend_designer")
    mock_pt.assert_called_once()


def test_agent_worker_returns_idle_on_heartbeat():
    sys.modules.pop("agents.agent_worker", None)
    from agents import agent_worker

    response = MagicMock()
    response.text = 'data: {"type":"heartbeat"}\n\n'
    response.raise_for_status.return_value = None

    with patch("httpx.get", return_value=response):
        result = agent_worker.run_once("qa_tester")

    assert result == {"ok": True, "status": "idle", "agent": "qa_tester"}


def test_agent_worker_rejects_unsupported_agent():
    sys.modules.pop("agents.agent_worker", None)
    from agents import agent_worker
    result = agent_worker.run_once("ghost_agent")
    assert result["ok"] is False
    assert result["status"] == "unsupported_agent"


def test_agent_worker_handles_poll_failure():
    sys.modules.pop("agents.agent_worker", None)
    from agents import agent_worker
    with patch("httpx.get", side_effect=Exception("connection refused")):
        result = agent_worker.run_once("devops_release")
    assert result["ok"] is False
    assert result["status"] == "poll_failed"
