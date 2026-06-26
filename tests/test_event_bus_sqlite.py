"""
Tests for infra/event_bus_sqlite.py.

No real SQLite DB — task_persistence is reset for each test so the
in-memory test DB stays isolated. FastAPI endpoints tested via TestClient.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

import task_persistence
import infra.event_bus_sqlite as eb


_APPR_HDR = "test-sqlite-bus-approval-token"
_AUTH_HEADERS = {"Authorization": f"Bearer {_APPR_HDR}"}


@pytest.fixture(autouse=True)
def _reset_db(tmp_path, monkeypatch):
    db = tmp_path / "test_bus.sqlite3"
    monkeypatch.setenv("JARVIS_TASK_DB_PATH", str(db))
    task_persistence._INITIALIZED = False
    yield
    task_persistence._INITIALIZED = False


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("JARVIS_EVENT_BUS_APPROVAL_TOKEN", _APPR_HDR)
    with TestClient(eb.app, raise_server_exceptions=True) as tc:
        yield tc


# ── GET /health ────────────────────────────────────────────────────────────────

class TestHealth:
    def test_returns_ok_with_sqlite_backend(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["backend"] == "sqlite"


# ── POST /tasks ────────────────────────────────────────────────────────────────

class TestPostTasks:
    def test_returns_202_and_task_id(self, client):
        resp = client.post("/tasks", json={
            "title": "Build API",
            "description": "Write /users endpoint",
            "agent": "backend_engineer",
            "priority": 7,
        })
        assert resp.status_code == 202
        body = resp.json()
        assert "task_id" in body
        assert body["agent"] == "backend_engineer"
        assert body["status"] == "queued"

    def test_task_persisted_in_sqlite(self, client):
        resp = client.post("/tasks", json={"title": "T", "description": "D", "agent": "qa_tester"})
        task_id = resp.json()["task_id"]
        snapshot = task_persistence.load_snapshot()
        ids = [t.get("id") for t in snapshot.get("tasks", [])]
        assert task_id in ids

    def test_task_initial_status_is_queued(self, client):
        resp = client.post("/tasks", json={"title": "T", "description": "D"})
        task_id = resp.json()["task_id"]
        snapshot = task_persistence.load_snapshot()
        task = next((t for t in snapshot["tasks"] if t["id"] == task_id), None)
        assert task is not None
        assert task["status"] == "queued"

    def test_default_agent_is_backend_engineer(self, client):
        resp = client.post("/tasks", json={"title": "T", "description": "D"})
        assert resp.json()["agent"] == "backend_engineer"

    def test_credential_pattern_blocked(self, client):
        resp = client.post("/tasks", json={
            "title": "Rotate key",
            "description": "Use " + "sk-ant-" + ("a" * 48) + " to call API",
            "agent": "backend_engineer",
        })
        assert resp.status_code == 202
        body = resp.json()
        assert body["queued"] is False
        assert body["status"] == "waiting_approval"

    def test_external_task_with_risky_keyword_held(self, client, monkeypatch):
        with patch("infra.event_bus_sqlite._inline_threat_screen",
                   wraps=eb._inline_threat_screen):
            resp = client.post("/tasks", json={
                "title": "Delete all",
                "description": "rm -rf /data",
                "agent": "devops_release",
                "context": {"source": "webhook"},
            })
        assert resp.status_code == 202
        body = resp.json()
        assert body["queued"] is False

    def test_clean_task_passes_through(self, client):
        resp = client.post("/tasks", json={
            "title": "Write unit tests",
            "description": "Add coverage for the new parser",
            "agent": "qa_tester",
        })
        assert resp.status_code == 202
        assert "task_id" in resp.json()


# ── GET /tasks/{task_id}/status ────────────────────────────────────────────────

class TestTaskStatus:
    def test_returns_queued_status(self, client):
        resp = client.post("/tasks", json={"title": "T", "description": "D"})
        task_id = resp.json()["task_id"]
        status_resp = client.get(f"/tasks/{task_id}/status")
        assert status_resp.status_code == 200
        data = status_resp.json()
        assert data["task_id"] == task_id
        assert data["status"] == "queued"

    def test_returns_404_for_unknown_task(self, client):
        resp = client.get("/tasks/nonexistent-task-xyz/status")
        assert resp.status_code == 404


# ── POST /results ──────────────────────────────────────────────────────────────

class TestPostResults:
    def test_marks_task_succeeded(self, client):
        create = client.post("/tasks", json={"title": "T", "description": "D"})
        task_id = create.json()["task_id"]
        resp = client.post("/results", json={
            "task_id": task_id, "output": "Done.", "needs_review": False,
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "queued"
        tasks = task_persistence.list_tasks_with_status("succeeded")
        assert any(t["id"] == task_id for t in tasks)

    def test_marks_task_waiting_approval_when_needs_review(self, client):
        create = client.post("/tasks", json={"title": "T", "description": "D"})
        task_id = create.json()["task_id"]
        client.post("/results", json={
            "task_id": task_id, "output": "risky output", "needs_review": True,
        })
        tasks = task_persistence.list_tasks_with_status("waiting_approval")
        assert any(t["id"] == task_id for t in tasks)

    def test_think_tags_stripped_from_output(self, client):
        create = client.post("/tasks", json={"title": "T", "description": "D"})
        task_id = create.json()["task_id"]
        client.post("/results", json={
            "task_id": task_id,
            "output": "<think>private reasoning</think>public answer",
            "needs_review": False,
        })
        tasks = task_persistence.list_tasks_with_status("succeeded")
        task = next(t for t in tasks if t["id"] == task_id)
        assert "private reasoning" not in task.get("result", "")
        assert "public answer" in task.get("result", "")


# ── Approvals ──────────────────────────────────────────────────────────────────

class TestApprovals:
    def _submit_and_flag(self, client) -> str:
        create = client.post("/tasks", json={"title": "T", "description": "D"})
        task_id = create.json()["task_id"]
        client.post("/results", json={
            "task_id": task_id, "output": "needs review", "needs_review": True,
        })
        return task_id

    def test_pending_approvals_returns_list(self, client):
        task_id = self._submit_and_flag(client)
        resp = client.get("/approvals/pending")
        assert resp.status_code == 200
        ids = [t.get("id") for t in resp.json()]
        assert task_id in ids

    def test_get_single_approval(self, client):
        task_id = self._submit_and_flag(client)
        resp = client.get(f"/approvals/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == task_id

    def test_get_approval_404_when_missing(self, client):
        resp = client.get("/approvals/no-such-task")
        assert resp.status_code == 404

    def test_approve_puts_task_back_to_queued(self, client):
        task_id = self._submit_and_flag(client)
        resp = client.post(
            f"/approvals/{task_id}",
            json={"decision": "approve", "reason": "ok"},
            headers=_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "approve"
        tasks = task_persistence.list_tasks_with_status("queued")
        assert any(t["id"] == task_id for t in tasks)

    def test_reject_marks_task_failed(self, client):
        task_id = self._submit_and_flag(client)
        client.post(
            f"/approvals/{task_id}",
            json={"decision": "reject", "reason": "dangerous"},
            headers=_AUTH_HEADERS,
        )
        tasks = task_persistence.list_tasks_with_status("failed")
        assert any(t["id"] == task_id for t in tasks)

    def test_dismiss_cancels_task(self, client):
        task_id = self._submit_and_flag(client)
        resp = client.delete(f"/approvals/{task_id}", headers=_AUTH_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["status"] == "dismissed"
        tasks = task_persistence.list_tasks_with_status("cancelled")
        assert any(t["id"] == task_id for t in tasks)

    def test_approve_without_token_returns_401(self, client):
        task_id = self._submit_and_flag(client)
        resp = client.post(f"/approvals/{task_id}", json={"decision": "approve"})
        assert resp.status_code == 401

    def test_invalid_decision_returns_422(self, client):
        task_id = self._submit_and_flag(client)
        resp = client.post(f"/approvals/{task_id}", json={"decision": "maybe"}, headers=_AUTH_HEADERS)
        assert resp.status_code == 422


# ── GET /agent/{name}/inbox (synchronous approximation) ───────────────────────

class TestAgentInbox:
    def test_heartbeat_when_no_tasks(self, client):
        resp = client.get("/agent/researcher/inbox", params={"timeout_ms": 100})
        assert resp.status_code == 200
        assert b"heartbeat" in resp.content

    def test_streams_task_from_queue(self, client, monkeypatch):
        import asyncio as _asyncio

        async def _fake_get_nowait(q=None):
            return {
                "id": "task_abc123",
                "agent": "researcher",
                "title": "Research topic",
                "status": "assigned",
            }

        # Pre-populate the agent queue directly
        loop = _asyncio.new_event_loop()
        try:
            async def _seed():
                q = eb._agent_queue("researcher")
                await q.put({
                    "id": "task_teststream",
                    "agent": "researcher",
                    "title": "Research topic",
                    "status": "assigned",
                })
            loop.run_until_complete(_seed())
        finally:
            loop.close()

        resp = client.get("/agent/researcher/inbox", params={"timeout_ms": 200})
        assert resp.status_code == 200
        body = resp.text
        assert "task_teststream" in body
        assert '"type": "task"' in body or '"type":"task"' in body


# ── GET /metrics ───────────────────────────────────────────────────────────────

class TestMetrics:
    def test_returns_task_counts(self, client):
        client.post("/tasks", json={"title": "T1", "description": "D", "agent": "qa_tester"})
        client.post("/tasks", json={"title": "T2", "description": "D", "agent": "researcher"})
        resp = client.get("/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "task_counts" in data
        assert data["backend"] == "sqlite"
        assert data["task_counts"].get("queued", 0) >= 2


# ── strip_think_tags + _sanitize_recursive ────────────────────────────────────

class TestPureFunctions:
    def test_strip_think_tags(self):
        assert eb.strip_think_tags("<think>hidden</think>visible") == "visible"

    def test_strip_thinking_variant(self):
        assert eb.strip_think_tags("<thinking>x</thinking>result") == "result"

    def test_no_tags_unchanged(self):
        assert eb.strip_think_tags("plain") == "plain"

    def test_sanitize_dict(self):
        d = {"output": "<think>x</think>ok", "other": "fine"}
        result = eb._sanitize_recursive(d)
        assert result["output"] == "ok"
        assert result["other"] == "fine"

    def test_sanitize_list(self):
        lst = ["<think>a</think>A", "<think>b</think>B"]
        assert eb._sanitize_recursive(lst) == ["A", "B"]
