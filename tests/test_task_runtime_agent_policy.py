from unittest.mock import patch

import task_runtime


def _fake_workspace() -> dict:
    return {
        "ok": False,
        "enabled": False,
        "created": False,
        "reason": "",
        "repo_root": "",
        "worktree_path": "",
        "branch": "",
    }


def test_dev_team_agent_runs_clear_work_with_confidence_debrief_contract():
    task_runtime.reset_for_tests()
    with patch("task_runtime.route_stream", return_value=(iter(["Fixed the targeted test."]), "UnitTestModel")), \
         patch("task_runtime.worktree_manager.prepare_isolated_workspace", return_value=_fake_workspace()):
        task = task_runtime.submit_task("fix the auth middleware unit test", kind="code")
        completed = task_runtime.wait_for_task(task["id"], timeout=2.0)

    assert completed is not None
    assert completed["assigned_agent_id"] == "backend-engineer"
    assert completed["autonomy"] == "auto_edit"
    assert completed["confidence"]["score"] >= completed["confidence"]["threshold"]
    assert completed["debrief_required"] is True
    assert "You are Backend Engineer" in completed["effective_prompt"]
    assert "Jarvis managed-agent debrief contract" in completed["effective_prompt"]
    assert completed["audit"]["manager_agent_id"] == "jarvis-manager"


def test_low_confidence_agent_work_waits_for_human_review():
    task_runtime.reset_for_tests()
    with patch("task_runtime.route_stream") as route_mock, \
         patch("task_runtime.worktree_manager.prepare_isolated_workspace", return_value=_fake_workspace()):
        task = task_runtime.submit_task("make everything better", kind="code")

    assert task["assigned_agent_id"] == "backend-engineer"
    assert task["status"] == "waiting_approval"
    assert task["approval_reason"] == "confidence below safe autonomy threshold"
    assert task["autonomy"] == "human_review"
    assert task["confidence"]["score"] < task["confidence"]["threshold"]
    route_mock.assert_not_called()
