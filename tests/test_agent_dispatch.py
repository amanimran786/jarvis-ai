import sys
import importlib
import pytest
from unittest.mock import MagicMock

# Mock heavy deps before any project imports
sys.modules.setdefault("ollama", MagicMock())
sys.modules.setdefault("memory", MagicMock())
sys.modules.setdefault("conversation_context", MagicMock())
sys.modules.setdefault("usage_tracker", MagicMock())

# Save the real config before mocking, so we can restore it after the
# module-level agent_dispatch import (prevents contaminating test files
# collected after this one, e.g. test_config_local_stt.py).
import config as _real_config  # noqa: E402

# Full 8-agent config stub — must use direct assignment (not setdefault) so
# this file's version wins regardless of pytest collection order.
_mock_config = MagicMock()
_mock_config.AGENT_ROSTER = {
    "backend_engineer":  {"role": "Backend engineer",   "tools": ["file_read", "file_write", "run_tests"],      "model": "glm-4.7-flash"},
    "frontend_designer": {"role": "Frontend designer",  "tools": ["code", "file_read", "file_write"],            "model": "glm-4.7-flash"},
    "ux_researcher":     {"role": "UX researcher",      "tools": ["web_search", "file_read"],                   "model": "glm-4.7-flash"},
    "security_reviewer": {"role": "Security reviewer",  "tools": ["file_read"],                                 "model": "glm-4.7-flash"},
    "qa_tester":         {"role": "QA tester",          "tools": ["code", "shell", "file_read"],                "model": "glm-4.7-flash"},
    "researcher":        {"role": "Researcher",         "tools": ["web_search", "file_read"],                   "model": "glm-4.7-flash"},
    "devops_release":    {"role": "DevOps release",     "tools": ["shell", "file_read", "file_write"],          "model": "glm-4.7-flash"},
    "memory_librarian":  {"role": "Memory librarian",   "tools": ["file_read", "file_write", "memory"],         "model": "glm-4.7-flash"},
}
sys.modules["config"] = _mock_config

# Pre-mock brain_ollama — only ensure the brains package exists.
# DO NOT install brains.brain_ollama at module level: that would contaminate
# any test file collected after this one (e.g. test_jarvis_regression_suite.py).
_mock_brain = MagicMock()
if not hasattr(sys.modules.get("brains"), "__path__"):
    sys.modules.pop("brains", None)
importlib.import_module("brains")

# Force fresh import so agent_dispatch sees our config mock, not a stale one
# baked in during earlier collection.
sys.modules["config"] = _mock_config
if "agent_dispatch" in sys.modules:
    del sys.modules["agent_dispatch"]

import agent_dispatch  # noqa: E402 — must come after sys.modules setup

# Restore real config so test files collected after this one (e.g.
# test_config_local_stt.py) see the real module, not a MagicMock.
sys.modules["config"] = _real_config

_STUB_MODS = {"config": _mock_config, "brains.brain_ollama": _mock_brain}


@pytest.fixture(autouse=True)
def _restore_stubs():
    """Install stubs before each test and restore original state on teardown."""
    saved = {k: sys.modules.get(k) for k in _STUB_MODS}
    for k, v in _STUB_MODS.items():
        sys.modules[k] = v
    yield
    for k, orig in saved.items():
        if orig is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = orig


def test_dispatch_returns_stream_for_known_agent():
    # Arrange — dispatch now uses ask_local_with_tools
    _mock_brain.ask_local_with_tools.return_value = iter(["Hello", " world"])
    # Act
    result = list(agent_dispatch.dispatch("backend_engineer", "Write a REST endpoint"))
    # Assert
    assert result == ["Hello", " world"]
    _mock_brain.ask_local_with_tools.assert_called_once()
    assert _mock_brain.ask_local_with_tools.call_args.kwargs["tools"] == [
        "read_file",
        "write_file",
        "run_tests",
    ]


def test_dispatch_raises_for_unknown_agent():
    with pytest.raises(RuntimeError, match="Unknown agent"):
        list(agent_dispatch.dispatch("nonexistent_agent", "some task"))


def test_list_agents_returns_all_eight():
    agents = agent_dispatch.list_agents()
    assert len(agents) == 8
    names = {a["name"] for a in agents}
    expected = {
        "backend_engineer",
        "frontend_designer",
        "ux_researcher",
        "security_reviewer",
        "qa_tester",
        "researcher",
        "devops_release",
        "memory_librarian",
    }
    assert names == expected
    for agent in agents:
        assert "role" in agent
        assert "model" in agent
