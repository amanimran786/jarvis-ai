"""Regression: agent tasks must NOT hit router.py fast-paths.

The bug: task_runtime._run_task called route_stream() which has conversational
fast-paths (_is_task_list_query, calendar, email, etc.). Agent prompts that
matched a fast-path pattern returned canned responses like "Open tasks: ..."
with model="Tasks" and prompt_tokens=0 instead of a real LLM response.

Fix: task_runtime imports smart_stream from model_router (no fast-paths).
"""

import sys
from unittest.mock import MagicMock, patch

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


def test_task_runtime_does_not_import_route_stream():
    """route_stream must not be reachable from task_runtime namespace."""
    assert not hasattr(task_runtime, "route_stream"), (
        "task_runtime.route_stream was found — fast-path contamination risk. "
        "Agent tasks must use smart_stream from model_router instead."
    )


def test_task_runtime_uses_smart_stream():
    """smart_stream must be the stream function used for agent task execution."""
    assert hasattr(task_runtime, "smart_stream"), (
        "task_runtime.smart_stream not found — agent tasks will break. "
        "Import smart_stream from model_router."
    )


def test_task_list_prompt_does_not_hit_fast_path():
    """An agent prompt containing 'tasks' must reach the LLM, not a fast-path."""
    task_runtime.reset_for_tests()

    real_response = "Here is my analysis of the current open tasks and recommended prioritisation."
    called_with: list[str] = []

    def fake_smart_stream(user_input, **kwargs):
        called_with.append(user_input)
        return iter([real_response]), "glm-4.7-flash"

    # This prompt would trigger _is_task_list_query in router.py if route_stream
    # were still in use — it must now go to smart_stream unchanged.
    contaminating_prompt = "list open tasks and tell me which ones are blocked"

    with patch("task_runtime.smart_stream", side_effect=fake_smart_stream), \
         patch("task_runtime.worktree_manager.prepare_isolated_workspace", return_value=_fake_workspace()):
        task = task_runtime.submit_task(contaminating_prompt, kind="research")
        completed = task_runtime.wait_for_task(task["id"], timeout=3.0)

    assert completed is not None, "task did not complete"
    assert completed["status"] == "succeeded", f"expected succeeded, got: {completed['status']}"
    assert real_response in completed["result"], (
        f"Agent got unexpected output — possible fast-path contamination.\n"
        f"Result: {completed['result']!r}"
    )
    # Confirm smart_stream was actually called (not bypassed)
    assert len(called_with) == 1, "smart_stream should have been called exactly once"
