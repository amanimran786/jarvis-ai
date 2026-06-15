"""Hermetic unit tests for project_manager.py.

The test DB lives in a temp directory; task_runtime is mocked so no real
model inference or threads run.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import time
import threading
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── Hermetic setup ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Each test gets its own SQLite file and a fresh project_manager module."""
    db_file = tmp_path / "test_pm.sqlite3"
    monkeypatch.setenv("JARVIS_PROJECT_DB_PATH", str(db_file))
    # Shorten polling for tests.
    monkeypatch.setenv("JARVIS_PROJECT_POLL_INTERVAL", "0.05")
    monkeypatch.setenv("JARVIS_PROJECT_HEARTBEAT", "9999")

    # Force reimport so the module-level _SCHEMA_INITIALIZED resets.
    sys.modules.pop("project_manager", None)
    import project_manager as pm
    pm._SCHEMA_INITIALIZED = False
    pm._EXECUTORS.clear()

    yield pm


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_proj(pm, title="Test Project", agent="backend-engineer", tasks=None):
    return pm.create_project(
        title=title,
        description="A test project",
        agent_id=agent,
        tasks=tasks or ["Step one", "Step two"],
    )


# ── Schema and creation ───────────────────────────────────────────────────────

class TestProjectCreation:
    def test_create_returns_dict_with_expected_keys(self, _isolated_db):
        pm = _isolated_db
        proj = _make_proj(pm)
        assert "id" in proj
        assert proj["title"] == "Test Project"
        assert proj["agent_id"] == "backend-engineer"
        assert proj["status"] == "pending"
        assert len(proj["tasks"]) == 2

    def test_task_seq_is_zero_indexed(self, _isolated_db):
        pm = _isolated_db
        proj = _make_proj(pm, tasks=["A", "B", "C"])
        seqs = [t["seq"] for t in proj["tasks"]]
        assert seqs == [0, 1, 2]

    def test_task_prompts_stored_correctly(self, _isolated_db):
        pm = _isolated_db
        proj = _make_proj(pm, tasks=["do thing 1", "do thing 2"])
        prompts = [t["prompt"] for t in proj["tasks"]]
        assert prompts == ["do thing 1", "do thing 2"]

    def test_task_dicts_with_title_and_prompt(self, _isolated_db):
        pm = _isolated_db
        tasks = [
            {"title": "Research", "prompt": "research the topic"},
            {"title": "Write", "prompt": "write the report"},
        ]
        proj = pm.create_project("Dict tasks", tasks=tasks)
        assert proj["tasks"][0]["title"] == "Research"
        assert proj["tasks"][1]["prompt"] == "write the report"

    def test_create_emits_project_created_event(self, _isolated_db):
        pm = _isolated_db
        proj = _make_proj(pm)
        events = pm.tail_events(proj["id"])
        types = [e["event_type"] for e in events]
        assert "project_created" in types


class TestProjectListing:
    def test_list_returns_created_projects(self, _isolated_db):
        pm = _isolated_db
        _make_proj(pm, title="Alpha")
        _make_proj(pm, title="Beta")
        projects = pm.list_projects()
        titles = {p["title"] for p in projects}
        assert "Alpha" in titles
        assert "Beta" in titles

    def test_list_filters_by_status(self, _isolated_db):
        pm = _isolated_db
        p1 = _make_proj(pm, title="Pending")
        _make_proj(pm, title="Other")
        pending = pm.list_projects(status="pending")
        ids = [p["id"] for p in pending]
        assert p1["id"] in ids

    def test_get_project_returns_none_for_unknown_id(self, _isolated_db):
        pm = _isolated_db
        assert pm.get_project("nonexistent_proj") is None

    def test_get_project_includes_tasks(self, _isolated_db):
        pm = _isolated_db
        proj = _make_proj(pm, tasks=["A", "B"])
        fetched = pm.get_project(proj["id"])
        assert fetched is not None
        assert len(fetched["tasks"]) == 2


# ── Dispatch and cancellation ────────────────────────────────────────────────

class TestDispatch:
    def test_dispatch_raises_for_unknown_project(self, _isolated_db):
        pm = _isolated_db
        with pytest.raises(ValueError, match="not found"):
            pm.dispatch_project("fake_id")

    def test_dispatch_raises_for_terminal_project(self, _isolated_db):
        pm = _isolated_db
        proj = _make_proj(pm)
        # Manually force terminal state.
        with pm._connect() as conn:
            conn.execute("UPDATE projects SET status='done' WHERE id=?", (proj["id"],))
        with pytest.raises(ValueError, match="already done"):
            pm.dispatch_project(proj["id"])

    def test_cancel_returns_false_for_terminal_project(self, _isolated_db):
        pm = _isolated_db
        proj = _make_proj(pm)
        with pm._connect() as conn:
            conn.execute("UPDATE projects SET status='done' WHERE id=?", (proj["id"],))
        result = pm.cancel_project(proj["id"])
        assert result is False

    def test_cancel_pending_project_sets_status(self, _isolated_db):
        pm = _isolated_db
        proj = _make_proj(pm)
        ok = pm.cancel_project(proj["id"])
        assert ok is True
        fetched = pm.get_project(proj["id"])
        assert fetched["status"] == "cancelled"

    def test_cancel_emits_event(self, _isolated_db):
        pm = _isolated_db
        proj = _make_proj(pm)
        pm.cancel_project(proj["id"])
        events = pm.tail_events(proj["id"])
        types = [e["event_type"] for e in events]
        assert "cancel_requested" in types


# ── Event tailing ─────────────────────────────────────────────────────────────

class TestEventTailing:
    def test_tail_returns_all_events(self, _isolated_db):
        pm = _isolated_db
        proj = _make_proj(pm)
        pm._emit(proj["id"], "custom_event", key="val")
        events = pm.tail_events(proj["id"])
        assert len(events) >= 2  # project_created + custom_event

    def test_tail_since_id_filters_older_events(self, _isolated_db):
        pm = _isolated_db
        proj = _make_proj(pm)
        first_batch = pm.tail_events(proj["id"])
        max_id = max(e["id"] for e in first_batch)
        pm._emit(proj["id"], "new_event")
        new_events = pm.tail_events(proj["id"], since_id=max_id)
        assert len(new_events) == 1
        assert new_events[0]["event_type"] == "new_event"


# ── Status rendering ─────────────────────────────────────────────────────────

class TestStatusRendering:
    def test_collect_status_returns_list(self, _isolated_db):
        pm = _isolated_db
        _make_proj(pm, title="My Project")
        rows = pm.collect_status()
        assert isinstance(rows, list)
        assert any(r["title"].startswith("My") for r in rows)

    def test_render_status_no_projects_returns_hint(self, _isolated_db):
        pm = _isolated_db
        output = pm.render_status([])
        assert "create" in output.lower()

    def test_render_status_shows_project_data(self, _isolated_db):
        pm = _isolated_db
        _make_proj(pm, title="Demo Project")
        rows = pm.collect_status()
        output = pm.render_status(rows)
        assert "Demo Project" in output
        assert "backend-engineer" in output


# ── Full autonomous execution (mocked task_runtime) ──────────────────────────

class TestAutonomousExecution:
    def _mock_task_runtime(self, pm, final_status="succeeded", result="done!"):
        """Patch task_runtime so submit_task returns immediately and get_task returns terminal."""
        fake_task_id = "task_abc123"
        mock_rt = MagicMock()
        mock_rt.submit_task.return_value = {"id": fake_task_id}
        mock_rt.get_task.return_value = {"id": fake_task_id, "status": final_status, "result": result}
        return mock_rt

    def test_project_completes_when_all_tasks_succeed(self, _isolated_db):
        pm = _isolated_db
        mock_rt = self._mock_task_runtime(pm)

        with patch.dict(sys.modules, {"task_runtime": mock_rt}):
            proj = _make_proj(pm, tasks=["Task A", "Task B"])
            pm.dispatch_project(proj["id"])

            # Wait for executor to finish (up to 5s).
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                current = pm.get_project(proj["id"])
                if current["status"] in ("done", "failed", "cancelled"):
                    break
                time.sleep(0.05)

        final = pm.get_project(proj["id"])
        assert final["status"] == "done"

    def test_project_fails_when_task_fails(self, _isolated_db):
        pm = _isolated_db
        mock_rt = self._mock_task_runtime(pm, final_status="failed", result="")

        with patch.dict(sys.modules, {"task_runtime": mock_rt}):
            proj = _make_proj(pm, tasks=["Task A"])
            pm.dispatch_project(proj["id"])

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                current = pm.get_project(proj["id"])
                if current["status"] in ("done", "failed", "cancelled"):
                    break
                time.sleep(0.05)

        final = pm.get_project(proj["id"])
        assert final["status"] == "failed"

    def test_task_events_emitted_on_success(self, _isolated_db):
        pm = _isolated_db
        mock_rt = self._mock_task_runtime(pm, final_status="succeeded", result="result text")

        with patch.dict(sys.modules, {"task_runtime": mock_rt}):
            proj = _make_proj(pm, tasks=["Only Task"])
            pm.dispatch_project(proj["id"])

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if pm.get_project(proj["id"])["status"] in ("done", "failed"):
                    break
                time.sleep(0.05)

        events = pm.tail_events(proj["id"])
        event_types = [e["event_type"] for e in events]
        assert "task_started" in event_types
        assert "task_done" in event_types
        assert "project_done" in event_types

    def test_tasks_run_sequentially(self, _isolated_db):
        pm = _isolated_db
        call_order: list[str] = []

        fake_task_id = "task_seq_test"
        mock_rt = MagicMock()

        def _fake_submit(prompt, **kwargs):
            call_order.append(prompt)
            return {"id": fake_task_id}

        mock_rt.submit_task.side_effect = _fake_submit
        mock_rt.get_task.return_value = {"id": fake_task_id, "status": "succeeded", "result": "ok"}

        with patch.dict(sys.modules, {"task_runtime": mock_rt}):
            proj = _make_proj(pm, tasks=["First", "Second", "Third"])
            pm.dispatch_project(proj["id"])

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if pm.get_project(proj["id"])["status"] in ("done", "failed"):
                    break
                time.sleep(0.05)

        assert call_order == ["First", "Second", "Third"]

    def test_dispatch_idempotent_returns_false_on_second_call(self, _isolated_db):
        pm = _isolated_db
        mock_rt = MagicMock()
        mock_rt.submit_task.return_value = {"id": "t_x"}
        mock_rt.get_task.return_value = {"id": "t_x", "status": "succeeded", "result": ""}

        with patch.dict(sys.modules, {"task_runtime": mock_rt}):
            proj = _make_proj(pm, tasks=["Only"])
            started1 = pm.dispatch_project(proj["id"])
            started2 = pm.dispatch_project(proj["id"])

        assert started1 is True
        assert started2 is False


# ── _looks_like_code_task heuristic ─────────────────────────────────────────

class TestCodeTaskHeuristic:
    def test_code_keywords_return_true(self, _isolated_db):
        pm = _isolated_db
        assert pm._looks_like_code_task("implement a sorting function")
        assert pm._looks_like_code_task("write a FastAPI endpoint")
        assert pm._looks_like_code_task("create a test for the parser")

    def test_non_code_prompts_return_false(self, _isolated_db):
        pm = _isolated_db
        assert not pm._looks_like_code_task("what time is it")
        assert not pm._looks_like_code_task("summarize the meeting notes")
        assert not pm._looks_like_code_task("who is the CEO of Apple")
