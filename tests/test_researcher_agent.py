"""
Tests for agents/researcher.py.

All external calls (web search, LLM, event bus, memory) are mocked.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

# ── Stubs before import ───────────────────────────────────────────────────────

_mock_config = MagicMock()
_mock_config.LOCAL_DEFAULT = "glm-4.7-flash"
_mock_config.AGENT_ROSTER = {
    "researcher": {
        "role": "Research agent.",
        "tools": ["web_search", "file_read"],
        "model": "glm-4.7-flash",
        "system_prompt": "Research Prompt",
    }
}

_STUB_MODS = ["config", "brains.brain_ollama"]
_AGENT_MODS = ["agents.researcher"]


_mock_brain = MagicMock()


@pytest.fixture(autouse=True)
def _restore_stubs():
    _saved = {k: sys.modules.get(k) for k in _STUB_MODS}
    sys.modules["config"] = _mock_config
    # Force-install (not setdefault) so real brain_ollama loaded by earlier tests is overridden
    sys.modules["brains.brain_ollama"] = _mock_brain
    for _mod in _AGENT_MODS:
        sys.modules.pop(_mod, None)
    yield
    for k, v in _saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
    for _mod in _AGENT_MODS:
        sys.modules.pop(_mod, None)


def _payload(task_id="t-1", title="Research AI trends", description="Find latest AI news", depth=2):
    return {
        "task_id": task_id,
        "title": title,
        "description": description,
        "context": {"session_id": "s1", "project_id": "p1", "research_depth": depth},
    }


def _cloud_payload(**kwargs):
    payload = _payload(**kwargs)
    payload["context"]["cloud_research_approved"] = True
    return payload


def _cloud_requested_payload(**kwargs):
    payload = _payload(**kwargs)
    payload["context"]["allow_cloud_research"] = True
    return payload


# ── Happy path via deep_research ─────────────────────────────────────────────

def test_uses_deep_research_when_cloud_research_is_enabled_and_approved():
    sys.modules.pop("agents.researcher", None)
    import agents.researcher as mod

    mock_result = {
        "report": "AI is growing fast.",
        "sources": [{"title": "TechCrunch", "url": "https://techcrunch.com", "snippet": "AI news"}],
        "queries_used": ["latest AI trends 2026"],
    }
    mock_store = MagicMock()
    mock_store.context_for_prompt.return_value = ""

    with patch.dict("os.environ", {"ALLOW_CLOUD_RESEARCH": "1"}), \
         patch.object(mod, "store", mock_store), \
         patch.object(mod, "deep_research", return_value=mock_result), \
         patch("httpx.post") as mock_post:
        mock_post.return_value.status_code = 202
        result = mod.process_task(_cloud_payload())

    assert "AI is growing fast." in result
    assert "TechCrunch" in result
    assert "latest AI trends 2026" in result


def test_includes_sources_in_output():
    sys.modules.pop("agents.researcher", None)
    import agents.researcher as mod

    mock_result = {
        "report": "Summary here.",
        "sources": [
            {"title": "Source A", "url": "https://a.com", "snippet": "..."},
            {"title": "Source B", "url": "https://b.com", "snippet": "..."},
        ],
        "queries_used": [],
    }
    with patch.dict("os.environ", {"ALLOW_CLOUD_RESEARCH": "1"}), \
         patch.object(mod, "store", None), \
         patch.object(mod, "deep_research", return_value=mock_result), \
         patch("httpx.post") as mock_post:
        mock_post.return_value.status_code = 202
        result = mod.process_task(_cloud_payload())

    assert "https://a.com" in result
    assert "https://b.com" in result


# ── Fallback when research pipeline unavailable ───────────────────────────────

def test_falls_back_to_llm_when_research_unavailable():
    sys.modules.pop("agents.researcher", None)
    import agents.researcher as mod

    _mock_brain.ask_local_stream.return_value = iter(["Fallback answer"])

    with patch.object(mod, "store", None), \
         patch.object(mod, "_RESEARCH_AVAILABLE", False), \
         patch("httpx.post") as mock_post:
        mock_post.return_value.status_code = 202
        result = mod.process_task(_payload())

    assert result == "Fallback answer"
    _mock_brain.ask_local_stream.assert_called_once()


def test_defaults_to_local_synthesis_without_cloud_approval():
    sys.modules.pop("agents.researcher", None)
    import agents.researcher as mod

    _mock_brain.ask_local_stream.return_value = iter(["Local answer"])

    with patch.object(mod, "store", None), \
         patch.object(mod, "deep_research", return_value={"report": "cloud", "sources": [], "queries_used": []}) as research_mock, \
         patch("httpx.post") as mock_post:
        mock_post.return_value.status_code = 202
        result = mod.process_task(_payload())

    assert result == "Local answer"
    research_mock.assert_not_called()


def test_cloud_research_requires_env_flag_even_when_task_context_approves():
    sys.modules.pop("agents.researcher", None)
    import agents.researcher as mod

    _mock_brain.ask_local_stream.return_value = iter(["Local answer"])

    with patch.dict("os.environ", {}, clear=True), \
         patch.object(mod, "store", None), \
         patch.object(mod, "deep_research", return_value={"report": "cloud", "sources": [], "queries_used": []}) as research_mock, \
         patch("httpx.post") as mock_post:
        mock_post.return_value.status_code = 202
        result = mod.process_task(_cloud_payload())

    assert result == "Local answer"
    research_mock.assert_not_called()


def test_cloud_research_requires_approval_not_just_request_flag():
    sys.modules.pop("agents.researcher", None)
    import agents.researcher as mod

    _mock_brain.ask_local_stream.return_value = iter(["Local answer"])

    with patch.dict("os.environ", {"ALLOW_CLOUD_RESEARCH": "1"}), \
         patch.object(mod, "store", None), \
         patch.object(mod, "deep_research", return_value={"report": "cloud", "sources": [], "queries_used": []}) as research_mock, \
         patch("httpx.post") as mock_post:
        mock_post.return_value.status_code = 202
        result = mod.process_task(_cloud_requested_payload())

    assert result == "Local answer"
    research_mock.assert_not_called()


def test_handles_deep_research_exception_gracefully():
    sys.modules.pop("agents.researcher", None)
    import agents.researcher as mod

    with patch.dict("os.environ", {"ALLOW_CLOUD_RESEARCH": "1"}), \
         patch.object(mod, "store", None), \
         patch.object(mod, "deep_research", side_effect=RuntimeError("search timeout")), \
         patch("httpx.post") as mock_post:
        mock_post.return_value.status_code = 202
        result = mod.process_task(_cloud_payload())

    assert "search timeout" in result


# ── Memory recall ─────────────────────────────────────────────────────────────

def test_memory_context_prepended_to_topic():
    sys.modules.pop("agents.researcher", None)
    import agents.researcher as mod

    mock_store = MagicMock()
    mock_store.context_for_prompt.return_value = "prior research: X was found useful"

    captured_topic = []

    def capture_research(topic, **kw):
        captured_topic.append(topic)
        return {"report": "R", "sources": [], "queries_used": []}

    with patch.dict("os.environ", {"ALLOW_CLOUD_RESEARCH": "1"}), \
         patch.object(mod, "store", mock_store), \
         patch.object(mod, "deep_research", side_effect=capture_research), \
         patch("httpx.post") as mock_post:
        mock_post.return_value.status_code = 202
        mod.process_task(_cloud_payload())

    assert "prior research: X was found useful" in captured_topic[0]


def test_memory_recall_failure_does_not_abort():
    sys.modules.pop("agents.researcher", None)
    import agents.researcher as mod

    mock_store = MagicMock()
    mock_store.context_for_prompt.side_effect = RuntimeError("qdrant down")

    with patch.dict("os.environ", {"ALLOW_CLOUD_RESEARCH": "1"}), \
         patch.object(mod, "store", mock_store), \
         patch.object(mod, "deep_research", return_value={"report": "ok", "sources": [], "queries_used": []}), \
         patch("httpx.post") as mock_post:
        mock_post.return_value.status_code = 202
        result = mod.process_task(_cloud_payload())

    assert result  # did not crash


# ── Event bus posting ─────────────────────────────────────────────────────────

def test_posts_result_to_event_bus():
    sys.modules.pop("agents.researcher", None)
    import agents.researcher as mod

    with patch.dict("os.environ", {"ALLOW_CLOUD_RESEARCH": "1"}), \
         patch.object(mod, "store", None), \
         patch.object(mod, "deep_research", return_value={"report": "done", "sources": [], "queries_used": []}), \
         patch("httpx.post") as mock_post:
        mock_post.return_value.status_code = 202
        mod.process_task(_cloud_payload(task_id="task-xyz"))

    assert mock_post.called
    posted = mock_post.call_args[1]["json"]
    assert posted["task_id"] == "task-xyz"
    assert posted["needs_review"] is False


def test_posts_with_correct_agent_id_header():
    sys.modules.pop("agents.researcher", None)
    import agents.researcher as mod

    with patch.dict("os.environ", {"ALLOW_CLOUD_RESEARCH": "1"}), \
         patch.object(mod, "store", None), \
         patch.object(mod, "deep_research", return_value={"report": "r", "sources": [], "queries_used": []}), \
         patch("httpx.post") as mock_post:
        mock_post.return_value.status_code = 202
        mod.process_task(_cloud_payload())

    assert mock_post.call_args[1]["headers"]["X-Jarvis-Agent-ID"] == "researcher"


def test_event_bus_failure_does_not_raise():
    sys.modules.pop("agents.researcher", None)
    import agents.researcher as mod

    with patch.dict("os.environ", {"ALLOW_CLOUD_RESEARCH": "1"}), \
         patch.object(mod, "store", None), \
         patch.object(mod, "deep_research", return_value={"report": "r", "sources": [], "queries_used": []}), \
         patch("httpx.post", side_effect=Exception("bus down")):
        result = mod.process_task(_cloud_payload())

    assert result  # still returns output


# ── Depth parameter ───────────────────────────────────────────────────────────

def test_research_depth_from_context():
    sys.modules.pop("agents.researcher", None)
    import agents.researcher as mod

    called_with = {}

    def capture(topic, depth=3):
        called_with["depth"] = depth
        return {"report": "r", "sources": [], "queries_used": []}

    with patch.dict("os.environ", {"ALLOW_CLOUD_RESEARCH": "1"}), \
         patch.object(mod, "store", None), \
         patch.object(mod, "deep_research", side_effect=capture), \
         patch("httpx.post") as mock_post:
        mock_post.return_value.status_code = 202
        mod.process_task(_cloud_payload(depth=5))

    assert called_with["depth"] == 5


# ── agent_worker integration ──────────────────────────────────────────────────

def test_researcher_in_supported_agents():
    sys.modules.pop("agents.agent_worker", None)
    from agents.agent_worker import _SUPPORTED_AGENTS
    assert "researcher" in _SUPPORTED_AGENTS


def test_agent_worker_dispatches_to_researcher():
    # Import without popping — patch works on the live cached module object
    import agents.agent_worker as aw
    from unittest.mock import MagicMock, patch

    event = (
        'data: {"type":"task","task_id":"t-r","task":'
        '{"title":"Research quantum","description":"Find quantum computing news","context":{}}}\n\n'
    )
    response = MagicMock()
    response.text = event
    response.raise_for_status.return_value = None

    mock_pt = MagicMock(return_value="research done")

    with patch("httpx.get", return_value=response), \
         patch.object(aw, "_load_process_task", return_value=mock_pt) as mock_load:
        result = aw.run_once("researcher")

    assert result["ok"] is True
    assert result["status"] == "processed"
    mock_load.assert_called_once_with("researcher")
