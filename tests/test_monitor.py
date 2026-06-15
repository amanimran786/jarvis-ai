"""Tests for monitor.py — rendering and data collection helpers.

All external dependencies (project_manager, orchestrate, task_runtime) are
mocked so the tests run without a live DB or Ollama.
"""
from __future__ import annotations

import sys
import json
import os
from unittest.mock import MagicMock, patch

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _load_monitor(monkeypatch):
    sys.modules.pop("monitor", None)
    # Suppress clear() in tests.
    monkeypatch.setenv("JARVIS_MONITOR_NO_CLEAR", "1")
    import monitor
    return monitor


@pytest.fixture()
def sample_projects():
    return [
        {
            "id": "proj_abc",
            "title": "Security Audit",
            "agent": "security-reviewer",
            "status": "running",
            "running": True,
            "tasks_total": 3,
            "tasks_done": 1,
            "tasks_failed": 0,
            "created_at": "2026-06-15T01:00:00",
            "updated_at": "2026-06-15T01:05:00",
        },
        {
            "id": "proj_def",
            "title": "Refactor auth module",
            "agent": "backend-engineer",
            "status": "done",
            "running": False,
            "tasks_total": 2,
            "tasks_done": 2,
            "tasks_failed": 0,
            "created_at": "2026-06-15T00:30:00",
            "updated_at": "2026-06-15T00:55:00",
        },
    ]


@pytest.fixture()
def sample_lanes():
    return [
        {
            "lane": "feature-x",
            "status": "RUNNING",
            "alive": True,
            "retries": 0,
            "files": 3,
            "lines": 42,
            "ready_to_sync": False,
            "prompt": "Add feature X to the API",
        },
        {
            "lane": "bugfix-y",
            "status": "DONE",
            "alive": False,
            "retries": 1,
            "files": 1,
            "lines": 5,
            "ready_to_sync": True,
            "prompt": "Fix bug Y in the parser",
        },
    ]


@pytest.fixture()
def sample_tasks():
    return {
        "active": [
            {"id": "task_001", "assigned_agent_id": "backend-engineer",
             "status": "running", "prompt": "Write a sorting function"},
        ],
        "busy_agents": [
            {"id": "backend-engineer", "status": "busy"},
        ],
        "total": 5,
    }


# ── _render_projects ──────────────────────────────────────────────────────────

class TestRenderProjects:
    def test_empty_returns_no_projects(self, _load_monitor):
        m = _load_monitor
        out = m._render_projects([])
        assert "no projects" in out.lower()

    def test_shows_project_title(self, _load_monitor, sample_projects):
        out = _load_monitor._render_projects(sample_projects)
        assert "Security Audit" in out

    def test_shows_agent(self, _load_monitor, sample_projects):
        out = _load_monitor._render_projects(sample_projects)
        assert "security-reviewer" in out

    def test_running_project_has_live_dot(self, _load_monitor, sample_projects):
        out = _load_monitor._render_projects(sample_projects)
        assert "●" in out  # live indicator for running project

    def test_done_project_shows_check(self, _load_monitor, sample_projects):
        out = _load_monitor._render_projects(sample_projects)
        assert "✓" in out

    def test_error_row_shows_error_message(self, _load_monitor):
        m = _load_monitor
        out = m._render_projects([{"_error": "import failed"}])
        assert "import failed" in out


# ── _render_lanes ──────────────────────────────────────────────────────────────

class TestRenderLanes:
    def test_empty_returns_no_lanes(self, _load_monitor):
        out = _load_monitor._render_lanes([])
        assert "no ade lanes" in out.lower()

    def test_shows_lane_name(self, _load_monitor, sample_lanes):
        out = _load_monitor._render_lanes(sample_lanes)
        assert "feature-x" in out

    def test_alive_lane_has_live_dot(self, _load_monitor, sample_lanes):
        out = _load_monitor._render_lanes(sample_lanes)
        assert "●" in out

    def test_sync_ready_lane_has_triangle(self, _load_monitor, sample_lanes):
        out = _load_monitor._render_lanes(sample_lanes)
        assert "▲" in out


# ── _render_tasks ──────────────────────────────────────────────────────────────

class TestRenderTasks:
    def test_empty_returns_no_tasks(self, _load_monitor):
        out = _load_monitor._render_tasks({"active": [], "busy_agents": [], "total": 0})
        assert "no active" in out.lower()

    def test_shows_agent_and_prompt(self, _load_monitor, sample_tasks):
        out = _load_monitor._render_tasks(sample_tasks)
        assert "backend-engineer" in out
        assert "sorting" in out

    def test_error_row(self, _load_monitor):
        out = _load_monitor._render_tasks({"_error": "boom", "active": [], "busy_agents": [], "total": 0})
        assert "boom" in out


# ── _render_summary ────────────────────────────────────────────────────────────

class TestRenderSummary:
    def test_all_idle_when_nothing_active(self, _load_monitor):
        tasks = {"active": [], "busy_agents": [], "total": 0}
        out = _load_monitor._render_summary([], [], tasks)
        assert "idle" in out.lower()

    def test_shows_running_projects_count(self, _load_monitor, sample_projects, sample_lanes, sample_tasks):
        out = _load_monitor._render_summary(sample_projects, sample_lanes, sample_tasks)
        assert "projects running" in out.lower()

    def test_shows_sync_ready_lanes(self, _load_monitor, sample_projects, sample_lanes):
        tasks = {"active": [], "busy_agents": [], "total": 0}
        out = _load_monitor._render_summary(sample_projects, sample_lanes, tasks)
        assert "harvest" in out.lower()


# ── render_dashboard ──────────────────────────────────────────────────────────

class TestRenderDashboard:
    def test_contains_header(self, _load_monitor, sample_projects, sample_lanes, sample_tasks):
        out = _load_monitor.render_dashboard(
            sample_projects, sample_lanes, sample_tasks
        )
        assert "JARVIS AGENT MONITOR" in out

    def test_contains_all_sections_by_default(self, _load_monitor, sample_projects, sample_lanes, sample_tasks):
        out = _load_monitor.render_dashboard(
            sample_projects, sample_lanes, sample_tasks
        )
        assert "PROJECTS" in out
        assert "ADE LANES" in out
        assert "ACTIVE TASKS" in out

    def test_projects_only_omits_lanes(self, _load_monitor, sample_projects, sample_lanes, sample_tasks):
        out = _load_monitor.render_dashboard(
            sample_projects, sample_lanes, sample_tasks,
            show_lanes=False,
            show_tasks=False,
        )
        assert "PROJECTS" in out
        assert "ADE LANES" not in out

    def test_icon_for_running_status(self, _load_monitor):
        assert _load_monitor._icon("running") == "▶"
        assert _load_monitor._icon("done") == "✓"
        assert _load_monitor._icon("failed") == "✗"
        assert _load_monitor._icon("pending") == "⏸"
