"""Hermetic unit tests for project_manager.py.

The test DB lives in a temp directory; task_runtime is mocked so no real
model inference or threads run.
"""
from __future__ import annotations

import sys
import time
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


# ── Parallel / dependency-aware execution ────────────────────────────────────

class TestParallelExecution:
    """Tests for the wave-scheduler added in Build 1.

    Two flavours of mock_rt are used:
      - _fast_rt: every task succeeds immediately (synchronous return).
      - _tracking_rt: records which task IDs were submitted concurrently.
    """

    def _fast_rt(self, final_status: str = "succeeded") -> MagicMock:
        counter = {"n": 0}
        mock_rt = MagicMock()

        def _submit(prompt, **kwargs):
            counter["n"] += 1
            return {"id": f"t_{counter['n']}"}

        def _get(task_id):
            return {"id": task_id, "status": final_status, "result": "ok"}

        mock_rt.submit_task.side_effect = _submit
        mock_rt.get_task.side_effect = _get
        return mock_rt

    def _wait(self, pm, project_id: str, timeout: float = 5.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = pm.get_project(project_id)["status"]
            if status in ("done", "failed", "cancelled"):
                return status
            time.sleep(0.05)
        return pm.get_project(project_id)["status"]

    def test_two_independent_tasks_both_complete(self, _isolated_db):
        pm = _isolated_db
        mock_rt = self._fast_rt()

        with patch.dict(sys.modules, {"task_runtime": mock_rt}):
            proj = pm.create_project(
                "parallel test",
                tasks=[
                    {"prompt": "alpha", "depends_on": []},
                    {"prompt": "beta", "depends_on": []},
                ],
            )
            pm.dispatch_project(proj["id"])
            status = self._wait(pm, proj["id"])

        assert status == "done"
        assert mock_rt.submit_task.call_count == 2

    def test_explicit_depends_on_enforces_ordering(self, _isolated_db):
        pm = _isolated_db
        call_order: list[str] = []
        mock_rt = MagicMock()

        def _submit(prompt, **kwargs):
            call_order.append(prompt)
            return {"id": f"t_{len(call_order)}"}

        mock_rt.submit_task.side_effect = _submit
        mock_rt.get_task.side_effect = lambda tid: {"id": tid, "status": "succeeded", "result": ""}

        with patch.dict(sys.modules, {"task_runtime": mock_rt}):
            proj = pm.create_project(
                "dep chain",
                tasks=[
                    {"prompt": "step0", "depends_on": []},
                    {"prompt": "step1", "depends_on": [0]},
                    {"prompt": "step2", "depends_on": [1]},
                ],
            )
            pm.dispatch_project(proj["id"])
            self._wait(pm, proj["id"])

        assert call_order == ["step0", "step1", "step2"]

    def test_fail_fast_on_parallel_failure(self, _isolated_db):
        pm = _isolated_db
        mock_rt = MagicMock()
        counter = {"n": 0}

        def _submit(prompt, **kwargs):
            counter["n"] += 1
            return {"id": f"t_{counter['n']}"}

        def _get(task_id):
            # First task fails, second succeeds — project must fail regardless.
            if task_id == "t_1":
                return {"id": task_id, "status": "failed", "result": ""}
            return {"id": task_id, "status": "succeeded", "result": "ok"}

        mock_rt.submit_task.side_effect = _submit
        mock_rt.get_task.side_effect = _get

        with patch.dict(sys.modules, {"task_runtime": mock_rt}):
            proj = pm.create_project(
                "fail-fast parallel",
                tasks=[
                    {"prompt": "will-fail", "depends_on": []},
                    {"prompt": "ok-task", "depends_on": []},
                ],
            )
            pm.dispatch_project(proj["id"])
            status = self._wait(pm, proj["id"])

        assert status == "failed"

    def test_implicit_sequential_default_unchanged(self, _isolated_db):
        # Plain string tasks (no depends_on key) must still run in seq order.
        pm = _isolated_db
        call_order: list[str] = []
        mock_rt = MagicMock()

        def _submit(prompt, **kwargs):
            call_order.append(prompt)
            return {"id": f"t_{len(call_order)}"}

        mock_rt.submit_task.side_effect = _submit
        mock_rt.get_task.side_effect = lambda tid: {"id": tid, "status": "succeeded", "result": ""}

        with patch.dict(sys.modules, {"task_runtime": mock_rt}):
            proj = pm.create_project("implicit seq", tasks=["A", "B", "C"])
            pm.dispatch_project(proj["id"])
            self._wait(pm, proj["id"])

        assert call_order == ["A", "B", "C"]

    def test_deadlock_fails_project_with_error_event(self, _isolated_db):
        pm = _isolated_db
        mock_rt = self._fast_rt()

        with patch.dict(sys.modules, {"task_runtime": mock_rt}):
            proj = pm.create_project(
                "deadlock",
                tasks=[
                    # Task 0 depends on seq 99 which doesn't exist.
                    {"prompt": "impossible", "depends_on": [99]},
                ],
            )
            pm.dispatch_project(proj["id"])
            status = self._wait(pm, proj["id"])

        assert status == "failed"
        events = pm.tail_events(proj["id"])
        error_events = [e for e in events if e["event_type"] == "project_error"]
        assert error_events, "project_error event should be emitted for unsatisfiable deps"

    def test_depends_on_stored_and_retrievable(self, _isolated_db):
        pm = _isolated_db
        proj = pm.create_project(
            "dep storage",
            tasks=[
                {"prompt": "first", "depends_on": []},
                {"prompt": "second", "depends_on": [0]},
            ],
        )
        tasks = proj["tasks"]
        assert tasks[0]["depends_on"] == "[]"
        assert tasks[1]["depends_on"] == "[0]"


# ── get_project include_events ────────────────────────────────────────────────

class TestGetProjectIncludeEvents:
    def test_default_returns_empty_events_list(self, _isolated_db):
        pm = _isolated_db
        proj = _make_proj(pm)
        fetched = pm.get_project(proj["id"])
        assert fetched["events"] == []

    def test_include_events_returns_recent_events(self, _isolated_db):
        pm = _isolated_db
        proj = _make_proj(pm)
        pm._emit(proj["id"], "custom_a")
        pm._emit(proj["id"], "custom_b")
        fetched = pm.get_project(proj["id"], include_events=10)
        types = [e["event_type"] for e in fetched["events"]]
        assert "custom_a" in types
        assert "custom_b" in types

    def test_include_events_limit_is_respected(self, _isolated_db):
        pm = _isolated_db
        proj = _make_proj(pm)
        for i in range(20):
            pm._emit(proj["id"], f"event_{i}")
        fetched = pm.get_project(proj["id"], include_events=5)
        assert len(fetched["events"]) == 5

    def test_include_events_returns_most_recent(self, _isolated_db):
        pm = _isolated_db
        proj = _make_proj(pm)
        for i in range(10):
            pm._emit(proj["id"], f"evt_{i}")
        fetched = pm.get_project(proj["id"], include_events=3)
        types = [e["event_type"] for e in fetched["events"]]
        assert types == ["evt_7", "evt_8", "evt_9"]


# ── retry_project ─────────────────────────────────────────────────────────────

class TestRetryProject:
    def _fast_rt(self, final_status: str = "succeeded") -> MagicMock:
        counter = {"n": 0}
        mock_rt = MagicMock()

        def _submit(prompt, **kwargs):
            counter["n"] += 1
            return {"id": f"t_{counter['n']}"}

        mock_rt.submit_task.side_effect = _submit
        mock_rt.get_task.side_effect = lambda tid: {"id": tid, "status": final_status, "result": "ok"}
        return mock_rt

    def _wait(self, pm, project_id: str, timeout: float = 5.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = pm.get_project(project_id)["status"]
            if status in ("done", "failed", "cancelled"):
                return status
            time.sleep(0.05)
        return pm.get_project(project_id)["status"]

    def test_retry_raises_for_non_failed_project(self, _isolated_db):
        pm = _isolated_db
        proj = _make_proj(pm)
        with pytest.raises(ValueError, match="failed or cancelled"):
            pm.retry_project(proj["id"])

    def test_retry_raises_for_unknown_project(self, _isolated_db):
        pm = _isolated_db
        with pytest.raises(ValueError, match="not found"):
            pm.retry_project("nonexistent")

    def test_retry_resets_failed_tasks_to_pending(self, _isolated_db):
        pm = _isolated_db
        mock_rt = self._fast_rt(final_status="failed")

        with patch.dict(sys.modules, {"task_runtime": mock_rt}):
            proj = pm.create_project("retry test", tasks=["Task A", "Task B"])
            pm.dispatch_project(proj["id"])
            self._wait(pm, proj["id"])

        # Project should be failed now; reset with a succeeding runtime.
        mock_rt2 = self._fast_rt(final_status="succeeded")
        with patch.dict(sys.modules, {"task_runtime": mock_rt2}):
            pm.retry_project(proj["id"])
            status = self._wait(pm, proj["id"])

        assert status == "done"

    def test_retry_preserves_done_tasks(self, _isolated_db):
        """Tasks that succeeded in wave 1 must not be re-run after retry."""
        pm = _isolated_db
        submit_calls: list[str] = []
        mock_rt = MagicMock()
        call_n = {"n": 0}

        def _submit(prompt, **kwargs):
            call_n["n"] += 1
            submit_calls.append(prompt)
            return {"id": f"t_{call_n['n']}"}

        # First run: task 0 succeeds, task 1 fails.
        def _get_first(tid):
            if tid == "t_1":
                return {"id": tid, "status": "succeeded", "result": "ok"}
            return {"id": tid, "status": "failed", "result": ""}

        mock_rt.submit_task.side_effect = _submit
        mock_rt.get_task.side_effect = _get_first

        with patch.dict(sys.modules, {"task_runtime": mock_rt}):
            proj = pm.create_project("preserve done", tasks=["alpha", "beta"])
            pm.dispatch_project(proj["id"])
            self._wait(pm, proj["id"])

        assert pm.get_project(proj["id"])["status"] == "failed"
        submit_calls.clear()

        # Second run after retry: only the failed task should be re-submitted.
        mock_rt.get_task.side_effect = lambda tid: {"id": tid, "status": "succeeded", "result": "ok"}

        with patch.dict(sys.modules, {"task_runtime": mock_rt}):
            pm.retry_project(proj["id"])
            self._wait(pm, proj["id"])

        # Only "beta" (task 1) should have been submitted again, not "alpha".
        assert submit_calls == ["beta"]

    def test_retry_emits_project_retry_event(self, _isolated_db):
        pm = _isolated_db
        mock_rt = self._fast_rt(final_status="failed")

        with patch.dict(sys.modules, {"task_runtime": mock_rt}):
            proj = pm.create_project("event test", tasks=["only"])
            pm.dispatch_project(proj["id"])
            self._wait(pm, proj["id"])

        mock_rt2 = self._fast_rt(final_status="succeeded")
        with patch.dict(sys.modules, {"task_runtime": mock_rt2}):
            pm.retry_project(proj["id"])
            self._wait(pm, proj["id"])

        events = pm.tail_events(proj["id"])
        assert any(e["event_type"] == "project_retry" for e in events)


# ── template parallel fan-out ─────────────────────────────────────────────────

class TestTemplateDependencies:
    def test_security_audit_template_has_parallel_scans(self, _isolated_db):
        pm = _isolated_db
        proj = pm.create_from_template("security-audit", target="api.py")
        tasks = proj["tasks"]
        # First 4 tasks must be independent (depends_on = "[]")
        for t in tasks[:4]:
            assert t["depends_on"] == "[]", f"task {t['seq']} should be independent"
        # Last task depends on [0,1,2,3]
        last = tasks[4]
        import json
        deps = json.loads(last["depends_on"])
        assert sorted(deps) == [0, 1, 2, 3]

    def test_api_review_template_checks_fan_out_after_enumerate(self, _isolated_db):
        pm = _isolated_db
        proj = pm.create_from_template("api-review", target="api.py")
        tasks = proj["tasks"]
        import json
        # Task 0 (enumerate) must be independent.
        assert tasks[0]["depends_on"] == "[]"
        # Tasks 1-3 all depend only on task 0.
        for t in tasks[1:4]:
            deps = json.loads(t["depends_on"])
            assert deps == [0], f"task {t['seq']} should depend only on task 0"
        # Task 4 (summarize) depends on 1,2,3.
        deps_last = json.loads(tasks[4]["depends_on"])
        assert sorted(deps_last) == [1, 2, 3]

    def test_target_substituted_in_task_prompts(self, _isolated_db):
        pm = _isolated_db
        proj = pm.create_from_template("security-audit", target="mymodule.py")
        for t in proj["tasks"]:
            assert "mymodule.py" in t["prompt"], f"task {t['seq']} missing target substitution"


# ── cancel cancels in-flight tasks ──────────────────────────────────────────

class TestCancelInFlight:
    def test_cancel_calls_cancel_task_for_in_flight_work(self, _isolated_db):
        """When a project is cancelled, in-flight task_runtime tasks should be cancelled."""
        pm = _isolated_db
        cancel_calls: list[str] = []
        mock_rt = MagicMock()
        call_n = {"n": 0}

        def _submit(prompt, **kwargs):
            call_n["n"] += 1
            return {"id": f"t_{call_n['n']}"}

        # Task never completes — always returns running so it stays in-flight.
        mock_rt.submit_task.side_effect = _submit
        mock_rt.get_task.side_effect = lambda tid: {"id": tid, "status": "running", "result": ""}
        mock_rt.cancel_task.side_effect = lambda tid: cancel_calls.append(tid)

        with patch.dict(sys.modules, {"task_runtime": mock_rt}):
            proj = pm.create_project("cancel test", tasks=[
                {"prompt": "long task", "depends_on": []},
            ])
            pm.dispatch_project(proj["id"])
            # Give the executor a moment to submit the task and get it in-flight.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                fetched = pm.get_project(proj["id"])
                if any(t["status"] == "running" for t in fetched["tasks"]):
                    break
                time.sleep(0.05)
            # Now cancel the project.
            pm.cancel_project(proj["id"])
            # Wait for the executor to emit project_cancelled (fired AFTER _cancel_in_flight).
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                events = pm.tail_events(proj["id"])
                if any(e["event_type"] == "project_cancelled" for e in events):
                    break
                time.sleep(0.05)

        assert cancel_calls, "cancel_task should have been called for in-flight tasks"


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


# ── Result injection into downstream prompts ─────────────────────────────────

class TestResultInjection:
    """Verify that completed dep results are appended to downstream task prompts."""

    def _make_rt(self, results_by_call: list[str]) -> MagicMock:
        """Return a mock task_runtime that returns successive results.

        ``results_by_call[i]`` is the result for the i-th submitted task.
        """
        counter = {"n": 0}
        stored: dict[str, str] = {}
        mock_rt = MagicMock()

        def _submit(prompt, **kwargs):
            idx = counter["n"]
            tid = f"t_{idx}"
            result = results_by_call[idx] if idx < len(results_by_call) else "ok"
            stored[tid] = {"prompt": prompt, "result": result}
            counter["n"] += 1
            return {"id": tid}

        def _get(task_id):
            entry = stored.get(task_id)
            if entry is None:
                return None
            return {"id": task_id, "status": "succeeded", "result": entry["result"]}

        mock_rt.submit_task.side_effect = _submit
        mock_rt.get_task.side_effect = _get
        mock_rt._stored = stored
        return mock_rt

    def _wait(self, pm, project_id: str, timeout: float = 5.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = pm.get_project(project_id)["status"]
            if status in ("done", "failed", "cancelled"):
                return status
            time.sleep(0.05)
        return pm.get_project(project_id)["status"]

    def test_dep_result_injected_into_synthesis_prompt(self, _isolated_db):
        pm = _isolated_db
        mock_rt = self._make_rt(["finding: SQL injection in login.py", "synthesis done"])

        with patch.dict(sys.modules, {"task_runtime": mock_rt}):
            proj = pm.create_project(
                "inject test",
                tasks=[
                    {"title": "scan", "prompt": "scan the code", "depends_on": []},
                    {"title": "synthesize", "prompt": "write a report", "depends_on": [0]},
                ],
            )
            pm.dispatch_project(proj["id"])
            status = self._wait(pm, proj["id"])

        assert status == "done"
        calls = mock_rt.submit_task.call_args_list
        # Second call is the synthesis task — it must include scan result.
        synth_prompt = calls[1][0][0]
        assert "== Results from prerequisite tasks ==" in synth_prompt
        assert "finding: SQL injection in login.py" in synth_prompt
        assert "=== scan ===" in synth_prompt

    def test_no_injection_for_fanout_tasks(self, _isolated_db):
        """Tasks with depends_on=[] (fan-out) should not get injected context."""
        pm = _isolated_db
        mock_rt = self._make_rt(["result A", "result B"])

        with patch.dict(sys.modules, {"task_runtime": mock_rt}):
            proj = pm.create_project(
                "fanout test",
                tasks=[
                    {"title": "task A", "prompt": "do A", "depends_on": []},
                    {"title": "task B", "prompt": "do B", "depends_on": []},
                ],
            )
            pm.dispatch_project(proj["id"])
            self._wait(pm, proj["id"])

        calls = mock_rt.submit_task.call_args_list
        # Neither prompt should have injection context.
        for call in calls:
            assert "== Results from prerequisite tasks ==" not in call[0][0]

    def test_no_injection_when_dep_has_empty_result(self, _isolated_db):
        """Empty dep results should not produce the injection header."""
        pm = _isolated_db
        mock_rt = self._make_rt(["", "synthesis"])

        with patch.dict(sys.modules, {"task_runtime": mock_rt}):
            proj = pm.create_project(
                "empty result test",
                tasks=[
                    {"title": "noop", "prompt": "do nothing", "depends_on": []},
                    {"title": "synth", "prompt": "summarize", "depends_on": [0]},
                ],
            )
            pm.dispatch_project(proj["id"])
            self._wait(pm, proj["id"])

        calls = mock_rt.submit_task.call_args_list
        synth_prompt = calls[1][0][0]
        assert "== Results from prerequisite tasks ==" not in synth_prompt

    def test_multiple_dep_results_all_injected(self, _isolated_db):
        """All explicit dep results should appear in the downstream prompt."""
        pm = _isolated_db
        mock_rt = self._make_rt(["subprocess finding", "eval finding", "summary done"])

        with patch.dict(sys.modules, {"task_runtime": mock_rt}):
            proj = pm.create_project(
                "multi-dep inject",
                tasks=[
                    {"title": "scan A", "prompt": "scan subprocess", "depends_on": []},
                    {"title": "scan B", "prompt": "scan eval", "depends_on": []},
                    {"title": "report", "prompt": "write report", "depends_on": [0, 1]},
                ],
            )
            pm.dispatch_project(proj["id"])
            self._wait(pm, proj["id"])

        calls = mock_rt.submit_task.call_args_list
        # The third submit is the report task (tasks 0 and 1 run in parallel).
        report_prompt = calls[2][0][0]
        assert "subprocess finding" in report_prompt
        assert "eval finding" in report_prompt
        assert "=== scan A ===" in report_prompt
        assert "=== scan B ===" in report_prompt
