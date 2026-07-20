"""
Tests for multi-step task progress persistence and /resume command.

Covers:
  - checkpoint_step / find_interrupted_tasks round-trip
  - operative.run_task checkpoints each step and marks task finished
  - operative.resume_task skips completed steps and runs remaining
  - router /resume detection and routing
  - startup announcement helper
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

import task_persistence


# ── Helpers ────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_db():
    """Wipe in-memory SQLite state between tests."""
    task_persistence.reset_for_tests()
    yield
    task_persistence.reset_for_tests()


def _make_task(run_id: str, task: str = "do something", steps_total: int = 3) -> dict:
    return {
        "id": run_id,
        "status": "running",
        "task": task,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "",
        "steps_total": steps_total,
        "steps_done": 0,
        "plan": [],
        "result": "",
    }


# ── checkpoint_step / find_interrupted_tasks ────────────────────────────────────

class TestCheckpointAndFind:
    def test_no_interrupted_tasks_initially(self):
        assert task_persistence.find_interrupted_tasks() == []

    def test_running_task_appears_in_interrupted(self):
        task_persistence.upsert_task(_make_task("run_abc123"))
        found = task_persistence.find_interrupted_tasks()
        assert len(found) == 1
        assert found[0]["id"] == "run_abc123"

    def test_succeeded_task_not_in_interrupted(self):
        t = _make_task("run_done")
        t["status"] = "succeeded"
        task_persistence.upsert_task(t)
        assert task_persistence.find_interrupted_tasks() == []

    def test_checkpoint_step_stored_in_step_events(self):
        task_persistence.upsert_task(_make_task("run_xyz"))
        task_persistence.checkpoint_step(
            run_id="run_xyz",
            step_number=1,
            description="Write haiku",
            tool="chat",
            ok=True,
            result="Silicon dreams",
        )
        found = task_persistence.find_interrupted_tasks()
        assert len(found) == 1
        events = found[0]["step_events"]
        assert len(events) == 1
        assert events[0]["step_number"] == 1
        assert events[0]["ok"] is True
        assert "Silicon dreams" in events[0]["result"]

    def test_multiple_checkpoints_ordered_by_step(self):
        task_persistence.upsert_task(_make_task("run_multi", steps_total=3))
        for n in [1, 2]:
            task_persistence.checkpoint_step("run_multi", n, f"step{n}", "chat", True, f"result{n}")
        found = task_persistence.find_interrupted_tasks()
        events = found[0]["step_events"]
        assert [e["step_number"] for e in events] == [1, 2]

    def test_result_truncated_to_500_chars(self):
        task_persistence.upsert_task(_make_task("run_long"))
        long_result = "x" * 1000
        task_persistence.checkpoint_step("run_long", 1, "long step", "chat", True, long_result)
        found = task_persistence.find_interrupted_tasks()
        assert len(found[0]["step_events"][0]["result"]) <= 500

    def test_find_returns_most_recent_first(self):
        old = _make_task("run_old")
        old["created_at"] = "2026-01-01T00:00:00+00:00"
        task_persistence.upsert_task(old)
        new = _make_task("run_new")
        new["created_at"] = "2026-06-01T00:00:00+00:00"
        task_persistence.upsert_task(new)
        found = task_persistence.find_interrupted_tasks()
        # ORDER BY created_at DESC → newer task first
        ids = [t["id"] for t in found]
        assert ids[0] == "run_new"


# ── operative.run_task checkpoints ─────────────────────────────────────────────

class TestRunTaskCheckpoints:
    def _make_step(self, number: int, desc: str = "step"):
        from task_planner import TaskStep
        return TaskStep(number=number, description=desc, tool="chat")

    def _run_with_mocks(self, task: str = "do things", n_steps: int = 2, fail_at: int = 0):
        steps = [self._make_step(i + 1, f"step{i+1}") for i in range(n_steps)]

        def _fake_plan(t):
            return steps

        def _fake_execute(step, step_results, run_id=""):
            if fail_at and step.number == fail_at:
                return False, "simulated failure"
            return True, f"result_{step.number}"

        with patch("operative.plan_task", side_effect=_fake_plan), \
             patch("operative.execute_step", side_effect=_fake_execute), \
             patch("operative._summarize", return_value="all done"), \
             patch("operative.preflect.is_enabled", return_value=False), \
             patch("operative.replan_after_failure", return_value=[]):
            import operative
            return operative.run_task(task)

    def test_task_marked_succeeded_in_db(self):
        self._run_with_mocks()
        # After completion, no running tasks should remain
        assert task_persistence.find_interrupted_tasks() == []

    def test_checkpoint_written_for_each_step(self):
        # Intercept checkpoint_step calls
        calls: list[dict] = []
        orig = task_persistence.checkpoint_step
        def _capturing_checkpoint(run_id, step_number, description, tool, ok, result):
            calls.append({"step_number": step_number, "ok": ok})
            return orig(run_id, step_number, description, tool, ok, result)

        with patch("task_persistence.checkpoint_step", side_effect=_capturing_checkpoint):
            self._run_with_mocks(n_steps=3)

        assert len(calls) == 3
        assert [c["step_number"] for c in calls] == [1, 2, 3]
        assert all(c["ok"] for c in calls)

    def test_failed_step_checkpointed_as_not_ok(self):
        calls: list[dict] = []
        orig = task_persistence.checkpoint_step
        def _cap(run_id, step_number, description, tool, ok, result):
            calls.append({"step_number": step_number, "ok": ok})
            return orig(run_id, step_number, description, tool, ok, result)

        with patch("task_persistence.checkpoint_step", side_effect=_cap):
            self._run_with_mocks(n_steps=2, fail_at=2)

        step2 = next(c for c in calls if c["step_number"] == 2)
        assert step2["ok"] is False


# ── operative.resume_task ───────────────────────────────────────────────────────

class TestResumeTask:
    def _seed_interrupted(self, run_id: str, task: str, n_steps: int, done: int) -> None:
        from task_planner import TaskStep
        steps = [TaskStep(number=i + 1, description=f"step{i+1}", tool="chat") for i in range(n_steps)]
        task_persistence.upsert_task({
            "id": run_id,
            "status": "running",
            "task": task,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "",
            "steps_total": n_steps,
            "steps_done": done,
            "plan": [{"number": s.number, "description": s.description, "tool": s.tool, "params": {}} for s in steps],
            "result": "",
        })
        for i in range(done):
            task_persistence.checkpoint_step(run_id, i + 1, f"step{i+1}", "chat", True, f"done_{i+1}")

    def test_resume_unknown_run_id_returns_error(self):
        import operative
        result = operative.resume_task("run_nonexistent")
        assert result["ok"] is False
        assert "No interrupted task" in result["summary"]

    def test_resume_skips_completed_steps(self):
        self._seed_interrupted("run_r1", "do three things", 3, 2)
        executed = []

        def _fake_execute(step, step_results, run_id=""):
            executed.append(step.number)
            return True, "ok"

        with patch("operative.execute_step", side_effect=_fake_execute), \
             patch("operative._summarize", return_value="resumed"), \
             patch("operative.preflect.is_enabled", return_value=False), \
             patch("operative.replan_after_failure", return_value=[]):
            import operative
            result = operative.resume_task("run_r1")

        assert executed == [3], f"Expected only step 3, got {executed}"
        assert result["ok"] is True

    def test_resume_marks_task_succeeded(self):
        self._seed_interrupted("run_r2", "do stuff", 2, 1)

        def _fake_execute(step, step_results, run_id=""):
            return True, "ok"

        with patch("operative.execute_step", side_effect=_fake_execute), \
             patch("operative._summarize", return_value="done"), \
             patch("operative.preflect.is_enabled", return_value=False), \
             patch("operative.replan_after_failure", return_value=[]):
            import operative
            operative.resume_task("run_r2")

        assert task_persistence.find_interrupted_tasks() == []

    def test_resume_injects_prior_results_for_downstream_steps(self):
        self._seed_interrupted("run_r3", "chain task", 2, 1)
        received_results: list[dict] = []

        def _fake_execute(step, step_results, run_id=""):
            received_results.append(dict(step_results))
            return True, "step2_out"

        with patch("operative.execute_step", side_effect=_fake_execute), \
             patch("operative._summarize", return_value="done"), \
             patch("operative.preflect.is_enabled", return_value=False), \
             patch("operative.replan_after_failure", return_value=[]):
            import operative
            operative.resume_task("run_r3")

        assert 1 in received_results[0], "Step 1's result should be injected before step 2 runs"


# ── /resume detection ───────────────────────────────────────────────────────────

class TestResumeDetection:
    def test_slash_resume(self):
        from router import _is_resume_command
        assert _is_resume_command("/resume") is True

    def test_slash_resume_with_run_id(self):
        from router import _is_resume_command
        assert _is_resume_command("/resume run_abc123") is True

    def test_resume_task_phrase(self):
        from router import _is_resume_command
        assert _is_resume_command("resume task") is True

    def test_resume_last_task(self):
        from router import _is_resume_command
        assert _is_resume_command("resume last task") is True

    def test_unrelated_not_matched(self):
        from router import _is_resume_command
        assert _is_resume_command("what tasks are pending") is False

    def test_route_stream_returns_operative_label(self):
        task_persistence.upsert_task({
            "id": "run_test",
            "status": "running",
            "task": "do stuff",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "",
            "steps_total": 1,
            "steps_done": 0,
            "plan": [{"number": 1, "description": "step1", "tool": "chat", "params": {}}],
            "result": "",
        })

        def _fake_resume(run_id, on_progress=None, cancel_event=None):
            if on_progress:
                on_progress("Resuming", "step1")
            return {"task": "do stuff", "steps": [], "summary": "resumed", "results": {}, "ok": True}

        with patch("operative.resume_task", side_effect=_fake_resume):
            from router import route_stream
            stream, label = route_stream("/resume")
        assert label == "Operative"
        out = "".join(stream)
        assert len(out) > 0

    def test_route_stream_no_interrupted_tasks(self):
        from router import route_stream
        stream, label = route_stream("/resume")
        assert label == "Operative"
        out = "".join(stream)
        assert "No interrupted" in out


# ── startup announcement ────────────────────────────────────────────────────────

class TestAnnounceInterruptedTasks:
    def test_no_output_when_no_interrupted_tasks(self, capsys):
        from main import _announce_interrupted_tasks
        _announce_interrupted_tasks()
        assert capsys.readouterr().out == ""

    def test_prints_notice_when_interrupted_tasks_exist(self, capsys):
        task_persistence.upsert_task(_make_task("run_crash", "research AI safety"))
        from main import _announce_interrupted_tasks
        _announce_interrupted_tasks()
        out = capsys.readouterr().out
        assert "interrupted" in out.lower()
        assert "run_crash" in out
        assert "/resume" in out
