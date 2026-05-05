"""Tests for repo-grounded Jarvis codebase introspection."""

from __future__ import annotations

import coder_workbench


def test_improvement_text_uses_git_and_benchmark_state(monkeypatch):
    monkeypatch.setattr(
        coder_workbench,
        "status",
        lambda: {
            "branch": "main",
            "head": "abc123 test head",
            "changed_files": [{"status": "M", "path": "router.py"}],
            "recommended_next": [
                {"command": "python3 -m pytest tests/test_router.py -q", "required": True}
            ],
        },
    )
    monkeypatch.setattr(
        coder_workbench,
        "_latest_benchmark",
        lambda: {
            "categories": {
                "conversation": {"passed": 78, "total": 79, "failed": 1},
                "voice": {"passed": 34, "total": 34, "failed": 0},
            }
        },
    )

    text = coder_workbench.improvement_text()

    assert "I inspected the repo state" in text
    assert "conversation 78/79" in text
    assert "router.py" in text
    assert "python3 -m pytest tests/test_router.py -q" in text
