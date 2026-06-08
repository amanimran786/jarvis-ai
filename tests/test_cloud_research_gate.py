"""
Regression tests for cloud-research approval boundaries.

These are intentionally isolated from tests/test_manager.py because Claude is
actively editing that file for sys.modules contamination cleanup.
"""
from __future__ import annotations

from core.manager import AgentTask, _task_requires_manager_gate


def test_manager_security_gates_cloud_research_request():
    task = AgentTask(
        title="Research current vendors",
        description="Compare cloud model providers.",
        agent="researcher",
        context={"allow_cloud_research": True},
    )

    assert _task_requires_manager_gate(task) is True
    assert task.needs_security_review is True


def test_manager_does_not_gate_normal_local_research_task():
    task = AgentTask(
        title="Summarize local docs",
        description="Use local repository context only.",
        agent="researcher",
        context={},
    )

    assert _task_requires_manager_gate(task) is False
    assert task.needs_security_review is False

