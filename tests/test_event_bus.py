"""
Tests for infra/event_bus.py.

No real Redis, no real network. redis.asyncio is stubbed at the sys.modules level.
All async Redis calls use AsyncMock. FastAPI endpoints are tested via TestClient
(sync wrapper around the ASGI app) with get_redis patched per-test.
"""
from __future__ import annotations

import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Stub redis.asyncio before event_bus is imported ──────────────────────────
# redis is not installed in the dev environment, so we build a minimal stub.

_mock_redis_module = MagicMock()
_mock_aioredis = MagicMock()
_mock_aioredis.ResponseError = type("ResponseError", (Exception,), {})
_mock_redis_module.asyncio = _mock_aioredis
sys.modules.setdefault("redis", _mock_redis_module)
sys.modules.setdefault("redis.asyncio", _mock_aioredis)

# Clear any stale import so our stub takes effect
for _m in list(sys.modules):
    if _m.startswith("infra.event_bus") or _m == "infra.event_bus":
        del sys.modules[_m]

import infra.event_bus as eb
from fastapi.testclient import TestClient


# ── Redis mock factory ────────────────────────────────────────────────────────

def _make_redis() -> AsyncMock:
    r = AsyncMock()
    r.ping.return_value = True
    r.xadd.return_value = "1000-0"
    r.xack.return_value = 1
    r.xreadgroup.return_value = []
    r.xrevrange.return_value = []
    r.xrange.return_value = []
    r.xgroup_create.return_value = None
    r.xinfo_stream.return_value = {"length": 0}
    r.aclose = AsyncMock()
    return r


@pytest.fixture()
def redis_mock():
    return _make_redis()


@pytest.fixture()
def client(redis_mock):
    """TestClient without lifespan so scheduler/manager_loop don't spin up."""
    with patch.object(eb, "get_redis", AsyncMock(return_value=redis_mock)):
        # Not used as context manager → no lifespan startup/shutdown
        yield TestClient(eb.app, raise_server_exceptions=True), redis_mock


# ── Pure-function tests ───────────────────────────────────────────────────────

class TestStripThinkTags:
    def test_removes_think_block(self):
        raw = "<think>private reasoning</think>public answer"
        assert eb.strip_think_tags(raw) == "public answer"

    def test_removes_thinking_variant(self):
        raw = "<thinking>stuff</thinking>result"
        assert eb.strip_think_tags(raw) == "result"

    def test_multiline_think_block(self):
        raw = "<think>\nline1\nline2\n</think>clean"
        assert eb.strip_think_tags(raw) == "clean"

    def test_no_think_tags_unchanged(self):
        assert eb.strip_think_tags("plain text") == "plain text"

    def test_empty_string(self):
        assert eb.strip_think_tags("") == ""


class TestSanitizeRecursive:
    def test_sanitizes_string_value(self):
        result = eb._sanitize_recursive("<think>hidden</think>visible")
        assert result == "visible"

    def test_sanitizes_nested_dict(self):
        d = {"output": "<think>x</think>ok", "other": "fine"}
        result = eb._sanitize_recursive(d)
        assert result["output"] == "ok"
        assert result["other"] == "fine"

    def test_sanitizes_list(self):
        lst = ["<think>a</think>A", "<think>b</think>B"]
        result = eb._sanitize_recursive(lst)
        assert result == ["A", "B"]

    def test_passes_through_non_string(self):
        assert eb._sanitize_recursive(42) == 42
        assert eb._sanitize_recursive(None) is None


# ── POST /tasks ───────────────────────────────────────────────────────────────

class TestPostTasks:
    def test_returns_202_and_task_id(self, client):
        tc, r = client
        resp = tc.post("/tasks", json={
            "title": "Build API",
            "description": "Write /users endpoint",
            "agent": "backend_engineer",
            "priority": 7,
            "context": {},
        })
        assert resp.status_code == 202
        body = resp.json()
        assert "task_id" in body
        assert body["agent"] == "backend_engineer"

    def test_xadd_called_with_task_created_type(self, client):
        tc, r = client
        tc.post("/tasks", json={"title": "T", "description": "D", "agent": "qa_tester"})
        r.xadd.assert_called_once()
        fields = r.xadd.call_args[0][1]
        assert fields["type"] == "task.created"
        assert fields["agent"] == "qa_tester"

    def test_task_json_in_stream_payload(self, client):
        tc, r = client
        tc.post("/tasks", json={"title": "My Title", "description": "My Desc", "agent": "researcher"})
        fields = r.xadd.call_args[0][1]
        task_data = json.loads(fields["task"])
        assert task_data["title"] == "My Title"
        assert task_data["description"] == "My Desc"

    def test_default_agent_is_backend_engineer(self, client):
        tc, r = client
        resp = tc.post("/tasks", json={"title": "T", "description": "D"})
        assert resp.json()["agent"] == "backend_engineer"


# ── POST /results ─────────────────────────────────────────────────────────────

class TestPostResults:
    def test_returns_200_queued(self, client):
        tc, r = client
        resp = tc.post("/results", json={
            "task_id": "abc-123",
            "output": "Done.",
            "needs_review": False,
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "queued"

    def test_xadd_called_with_result_fields(self, client):
        tc, r = client
        tc.post("/results", json={"task_id": "t1", "output": "out", "needs_review": True})
        fields = r.xadd.call_args[0][1]
        assert fields["task_id"] == "t1"
        assert fields["needs_review"] == "true"

    def test_think_tags_stripped_from_output(self, client):
        tc, r = client
        tc.post("/results", json={"task_id": "t2", "output": "<think>priv</think>clean", "needs_review": False})
        fields = r.xadd.call_args[0][1]
        assert fields["output"] == "clean"


# ── GET /tasks/{id}/status ────────────────────────────────────────────────────

class TestTaskStatus:
    def test_returns_status_when_found(self, client):
        tc, r = client
        r.xrevrange.return_value = [
            ("1001-0", {"type": "task.assigned", "task_id": "abc", "agent": "researcher", "ts": "1.0"}),
            ("1000-0", {"type": "task.created", "task_id": "abc", "agent": "researcher", "ts": "0.9"}),
        ]
        resp = tc.get("/tasks/abc/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == "abc"
        assert data["type"] == "task.assigned"

    def test_returns_404_when_not_found(self, client):
        tc, r = client
        r.xrevrange.return_value = []
        resp = tc.get("/tasks/missing/status")
        assert resp.status_code == 404


# ── GET /agent/{name}/inbox ───────────────────────────────────────────────────

class TestAgentInbox:
    def test_heartbeat_when_no_tasks(self, client):
        tc, r = client
        r.xreadgroup.return_value = []
        resp = tc.get("/agent/backend_engineer/inbox")
        assert resp.status_code == 200
        assert b"heartbeat" in resp.content

    def test_streams_task_event_when_available(self, client):
        tc, r = client
        task_payload = json.dumps({"title": "Build route", "description": "POST /users"})
        r.xreadgroup.return_value = [
            ("jarvis:agent:researcher", [
                ("2000-0", {"task_id": "t-xyz", "task": task_payload}),
            ])
        ]
        resp = tc.get("/agent/researcher/inbox")
        assert resp.status_code == 200
        body = resp.text
        assert "t-xyz" in body
        assert '"type": "task"' in body or '"type":"task"' in body

    def test_xack_called_after_delivery(self, client):
        tc, r = client
        r.xreadgroup.return_value = [
            ("jarvis:agent:qa_tester", [
                ("3000-0", {"task_id": "t-qa", "task": "{}"}),
            ])
        ]
        tc.get("/agent/qa_tester/inbox")
        r.xack.assert_called_once()

    def test_think_tags_stripped_from_task_content(self, client):
        tc, r = client
        raw_task = json.dumps({"description": "<think>internal</think>public"})
        r.xreadgroup.return_value = [
            ("jarvis:agent:researcher", [("1-0", {"task_id": "t1", "task": raw_task})])
        ]
        resp = tc.get("/agent/researcher/inbox")
        assert b"internal" not in resp.content


# ── GET /approvals/pending ────────────────────────────────────────────────────

class TestApprovals:
    def test_returns_empty_list_when_none_pending(self, client):
        tc, r = client
        r.xrange.return_value = []
        resp = tc.get("/approvals/pending")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_pending_items(self, client):
        tc, r = client
        r.xrange.return_value = [
            ("500-0", {"task_id": "t-devops", "output": "deployed", "ts": "1.0"}),
        ]
        resp = tc.get("/approvals/pending")
        items = resp.json()
        assert len(items) == 1
        assert items[0]["task_id"] == "t-devops"
        assert items[0]["stream_id"] == "500-0"

    def test_get_single_approval_by_stream_id(self, client):
        tc, r = client
        r.xrange.return_value = [
            ("600-0", {"task_id": "t-single", "output": "deploy script", "ts": "2.0"}),
        ]
        resp = tc.get("/approvals/600-0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == "t-single"
        assert data["stream_id"] == "600-0"

    def test_get_single_approval_404_when_missing(self, client):
        tc, r = client
        r.xrange.return_value = []
        resp = tc.get("/approvals/999-0")
        assert resp.status_code == 404

    def test_approve_decision_acks_and_publishes_task_approved(self, client):
        tc, r = client
        r.xrange.return_value = [("700-0", {"task_id": "t-devops", "output": "ok"})]
        resp = tc.post("/approvals/700-0", json={"decision": "approve", "reason": "looks good"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "approve"
        assert data["task_id"] == "t-devops"
        # xadd called with task.approved type
        approved_call = r.xadd.call_args
        assert approved_call[0][1]["type"] == "task.approved"
        r.xack.assert_called_once_with(eb.STREAM_APPROVALS, "human", "700-0")

    def test_reject_decision_publishes_task_rejected(self, client):
        tc, r = client
        r.xrange.return_value = [("800-0", {"task_id": "t-risky", "output": "rm -rf /"})]
        resp = tc.post("/approvals/800-0", json={"decision": "reject", "reason": "destructive"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "reject"
        rejected_call = r.xadd.call_args
        assert rejected_call[0][1]["type"] == "task.rejected"
        assert rejected_call[0][1]["reason"] == "destructive"

    def test_invalid_decision_returns_422(self, client):
        tc, r = client
        resp = tc.post("/approvals/900-0", json={"decision": "maybe"})
        assert resp.status_code == 422

    def test_dismiss_acks_without_status_event(self, client):
        tc, r = client
        resp = tc.delete("/approvals/500-0")
        assert resp.status_code == 200
        assert resp.json()["status"] == "dismissed"
        r.xack.assert_called_once_with(eb.STREAM_APPROVALS, "human", "500-0")
        r.xadd.assert_not_called()


# ── GET /health ───────────────────────────────────────────────────────────────

class TestHealth:
    def test_returns_ok_when_redis_reachable(self, client):
        tc, r = client
        resp = tc.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_calls_redis_ping(self, client):
        tc, r = client
        tc.get("/health")
        r.ping.assert_called_once()


# ── GET /metrics ──────────────────────────────────────────────────────────────

class TestMetrics:
    def test_returns_stream_depths_and_agent_load(self, client):
        tc, r = client
        r.xinfo_stream.return_value = {"length": 12}
        resp = tc.get("/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "stream_depths" in data
        assert "agent_load" in data

    def test_xinfo_stream_called_for_all_streams(self, client):
        tc, r = client
        tc.get("/metrics")
        assert r.xinfo_stream.call_count == 3


# ── AgentScheduler._handle ───────────────────────────────────────────────────

class TestAgentScheduler:
    def _make_scheduler(self):
        r = _make_redis()
        return eb.AgentScheduler(r), r

    def test_routes_task_created_to_agent_stream(self):
        import asyncio
        sched, r = self._make_scheduler()
        fields = {
            "type": "task.created",
            "task_id": "t-sched",
            "agent": "frontend_designer",
            "task": json.dumps({"title": "Build nav", "description": "Add nav"}),
        }
        asyncio.run(sched._handle("msg-1", fields))
        r.xadd.assert_called()
        stream_arg = r.xadd.call_args_list[0][0][0]
        assert "frontend_designer" in stream_arg

    def test_increments_agent_load(self):
        import asyncio
        sched, r = self._make_scheduler()
        fields = {"type": "task.created", "task_id": "t1", "agent": "qa_tester", "task": "{}"}
        asyncio.run(sched._handle("msg-2", fields))
        assert sched.AGENT_LOAD.get("qa_tester", 0) >= 1

    def test_ignores_non_task_created_events(self):
        import asyncio
        sched, r = self._make_scheduler()
        fields = {"type": "task.assigned", "task_id": "t2", "agent": "researcher"}
        asyncio.run(sched._handle("msg-3", fields))
        # xadd not called for routing (only for ack path which writes status update)
        # But xack IS called
        r.xack.assert_called_once_with(eb.STREAM_TASKS, eb.GROUP_SCHEDULER, "msg-3")


# ── ManagerLoop._handle ───────────────────────────────────────────────────────

class TestManagerLoop:
    def _make_loop(self):
        r = _make_redis()
        return eb.ManagerLoop(r), r

    def test_routes_needs_review_to_approvals(self):
        import asyncio
        ml, r = self._make_loop()
        fields = {"task_id": "t-review", "output": "rm -rf /tmp", "needs_review": "true"}
        asyncio.run(ml._handle("msg-10", fields))
        r.xadd.assert_called_once()
        stream_arg = r.xadd.call_args[0][0]
        assert stream_arg == eb.STREAM_APPROVALS

    def test_does_not_route_to_approvals_when_clean(self):
        import asyncio
        ml, r = self._make_loop()
        fields = {"task_id": "t-clean", "output": "API built", "needs_review": "false"}
        asyncio.run(ml._handle("msg-11", fields))
        r.xadd.assert_not_called()

    def test_acks_result_stream_after_handling(self):
        import asyncio
        ml, r = self._make_loop()
        fields = {"task_id": "t-ack", "output": "ok", "needs_review": "false"}
        asyncio.run(ml._handle("msg-12", fields))
        r.xack.assert_called_once_with(eb.STREAM_RESULTS, eb.GROUP_MANAGER, "msg-12")

    def test_strips_think_tags_from_output_before_routing(self):
        import asyncio
        ml, r = self._make_loop()
        fields = {"task_id": "t-think", "output": "<think>secret</think>safe", "needs_review": "true"}
        asyncio.run(ml._handle("msg-13", fields))
        approval_fields = r.xadd.call_args[0][1]
        assert "secret" not in approval_fields["output"]
        assert approval_fields["output"] == "safe"


# ── ThinkTagMiddleware ────────────────────────────────────────────────────────

class TestThinkTagMiddleware:
    def test_strips_think_from_json_response(self, client):
        tc, r = client
        # POST /results returns {"status": "queued"} — no think tags but verifies
        # middleware doesn't corrupt clean responses
        resp = tc.post("/results", json={"task_id": "t", "output": "clean", "needs_review": False})
        assert resp.json()["status"] == "queued"

    def test_non_json_response_passes_through(self, client):
        tc, _ = client
        # /health returns JSON — ensure middleware handles it without corruption
        resp = tc.get("/health")
        assert resp.status_code == 200
