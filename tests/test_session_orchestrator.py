from __future__ import annotations

import json

import session_orchestrator


def test_cli_add_task_creates_codex_gated_proposal(monkeypatch, tmp_path):
    queue_path = tmp_path / "WORK_QUEUE.json"
    queue_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(session_orchestrator, "WORK_QUEUE", queue_path)
    monkeypatch.setattr(session_orchestrator, "RICH", False)
    monkeypatch.setattr(session_orchestrator, "_append_master_log", lambda *_: None)
    monkeypatch.setattr(
        session_orchestrator.sys,
        "argv",
        [
            "session_orchestrator.py",
            "add-task",
            "claude-worker",
            "Implement bounded change",
            "2",
        ],
    )

    session_orchestrator.main()

    proposal = json.loads(queue_path.read_text(encoding="utf-8"))[0]
    assert proposal["session_name"] == "claude-worker"
    assert proposal["task"] == "Implement bounded change"
    assert proposal["priority"] == 2
    assert proposal["status"] == "proposed"
    assert proposal["proposed_by"] == "human_cli"
    assert proposal["requires_codex_assignment"] is True
    assert proposal["assigned_ai"] is None
    assert proposal["assigned_at"] is None
