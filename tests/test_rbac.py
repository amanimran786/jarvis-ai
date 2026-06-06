"""
Tests for infra/rbac.py — RBAC enforcement layer.

Mock pattern: swap sys.modules entries before importing rbac so no
real config or FastAPI is needed in the test environment.
"""
import sys
import time
import threading
from unittest.mock import MagicMock

# ── Stub config.AGENT_ROSTER before import ────────────────────────────────────

_mock_config = MagicMock()
_mock_config.AGENT_ROSTER = {
    "backend_engineer": {"tools": ["code", "shell", "file_read"], "model": "glm-4.7-flash"},
    "researcher":       {"tools": ["web_search", "file_read"],     "model": "glm-4.7-flash"},
    "qa_tester":        {"tools": ["code", "shell"],               "model": "glm-4.7-flash"},
}
sys.modules["config"] = _mock_config

# FastAPI stubs — HTTPException must be a real exception subclass
import fastapi as _fastapi  # already installed; just use the real one

if "infra.rbac" in sys.modules:
    del sys.modules["infra.rbac"]

from infra.rbac import RBACRegistry, AgentIdentity


def _make_registry() -> RBACRegistry:
    # Re-assert our stub each call — other test modules clobber sys.modules["config"]
    # during pytest collection and the last writer wins unless we restore here.
    sys.modules["config"] = _mock_config
    r = RBACRegistry.__new__(RBACRegistry)
    from infra.rbac import _SlidingWindow
    r._identities = {}
    r._rl = _SlidingWindow()
    r._seed()
    return r


def _fake_request(agent_id: str | None = None) -> MagicMock:
    req = MagicMock()
    req.headers = {}
    if agent_id:
        req.headers = {"X-Jarvis-Agent-ID": agent_id}
    return req


# ─── Identity resolution ──────────────────────────────────────────────────────

def test_no_header_resolves_to_human():
    reg = _make_registry()
    identity = reg.caller_from_request(_fake_request())
    assert identity.agent_id == "human"
    assert identity.role == "human"


def test_valid_agent_header_resolves():
    reg = _make_registry()
    identity = reg.caller_from_request(_fake_request("backend_engineer"))
    assert identity.agent_id == "backend_engineer"
    assert identity.role == "agent"


def test_unknown_agent_header_raises_403():
    import pytest
    reg = _make_registry()
    with pytest.raises(_fastapi.HTTPException) as exc_info:
        reg.caller_from_request(_fake_request("nonexistent_agent"))
    assert exc_info.value.status_code == 403


# ─── Dispatch permission ──────────────────────────────────────────────────────

def test_human_can_dispatch_any_agent():
    reg = _make_registry()
    human = reg.get("human")
    # Should not raise for any agent
    for name in ["backend_engineer", "researcher", "qa_tester", "jarvis_manager"]:
        reg.check_dispatch(human, name)


def test_manager_can_dispatch_any_agent():
    reg = _make_registry()
    manager = reg.get("jarvis_manager")
    reg.check_dispatch(manager, "backend_engineer")
    reg.check_dispatch(manager, "researcher")


def test_agent_cannot_dispatch_other_agents_by_default():
    import pytest
    reg = _make_registry()
    be = reg.get("backend_engineer")
    with pytest.raises(_fastapi.HTTPException) as exc_info:
        reg.check_dispatch(be, "researcher")
    assert exc_info.value.status_code == 403


def test_readonly_cannot_dispatch():
    import pytest
    from infra.rbac import AgentIdentity
    reg = _make_registry()
    readonly = AgentIdentity(
        agent_id="monitor",
        role="readonly",
        allowed_tools=[],
        allowed_agents=[],
        rate_limit_rpm=10,
    )
    with pytest.raises(_fastapi.HTTPException) as exc_info:
        reg.check_dispatch(readonly, "backend_engineer")
    assert exc_info.value.status_code == 403


# ─── Tool permission ──────────────────────────────────────────────────────────

def test_human_can_use_any_tool():
    reg = _make_registry()
    human = reg.get("human")
    reg.check_tool(human, "shell")
    reg.check_tool(human, "file_write")
    reg.check_tool(human, "anything")


def test_agent_can_use_allowed_tool():
    reg = _make_registry()
    be = reg.get("backend_engineer")
    reg.check_tool(be, "code")
    reg.check_tool(be, "file_read")


def test_agent_cannot_use_disallowed_tool():
    import pytest
    reg = _make_registry()
    be = reg.get("backend_engineer")
    with pytest.raises(_fastapi.HTTPException) as exc_info:
        reg.check_tool(be, "web_search")
    assert exc_info.value.status_code == 403


# ─── Rate limiting ────────────────────────────────────────────────────────────

def test_rate_limit_allows_up_to_limit():
    reg = _make_registry()
    # Give researcher a 5 rpm limit for this test
    reg._identities["researcher"].rate_limit_rpm = 5
    researcher = reg.get("researcher")
    for _ in range(5):
        reg.check_rate_limit(researcher)  # must not raise


def test_rate_limit_blocks_over_limit():
    import pytest
    reg = _make_registry()
    reg._identities["qa_tester"].rate_limit_rpm = 3
    qa = reg.get("qa_tester")
    for _ in range(3):
        reg.check_rate_limit(qa)
    with pytest.raises(_fastapi.HTTPException) as exc_info:
        reg.check_rate_limit(qa)
    assert exc_info.value.status_code == 429


def test_rate_limit_resets_after_window():
    """Calls older than 60s don't count against the window."""
    from infra.rbac import _SlidingWindow
    rl = _SlidingWindow()
    # Manually plant timestamps 61 seconds ago
    old = time.monotonic() - 61.0
    rl._windows["fake_agent"] = [old, old, old]
    assert rl.is_allowed("fake_agent", 3)  # window is clear — should pass


# ─── enforce() convenience ───────────────────────────────────────────────────

def test_enforce_returns_identity_on_success():
    reg = _make_registry()
    result = reg.enforce(_fake_request(), "backend_engineer")
    assert result.agent_id == "human"


def test_enforce_raises_for_agent_cross_dispatch():
    import pytest
    reg = _make_registry()
    with pytest.raises(_fastapi.HTTPException) as exc_info:
        reg.enforce(_fake_request("backend_engineer"), "researcher")
    assert exc_info.value.status_code == 403
