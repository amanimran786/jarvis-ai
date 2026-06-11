from __future__ import annotations

import json
import sys
import types
from unittest.mock import patch

from fastapi.testclient import TestClient


def test_security_audit_api_events_and_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SECURITY_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    import api
    from infra import security_audit as sa

    old_token = api._API_TOKEN
    api._API_TOKEN = "test-token"
    try:
        sa.audit_event(
            sa.CLOUD_CALL,
            actor="security_reviewer",
            target="security review fallback",
            decision="sent",
            severity="warning",
            reason="unit test",
            rollback_ref="manual_review",
        )
        client = TestClient(api.app)
        headers = {"Authorization": "Bearer test-token"}

        events = client.get("/security/events", headers=headers)
        assert events.status_code == 200
        body = events.json()
        assert body["ok"] is True
        assert body["events"][0]["action"] == sa.CLOUD_CALL

        filtered = client.get("/security/events?action=cloud_call", headers=headers)
        assert filtered.status_code == 200
        assert len(filtered.json()["events"]) == 1

        summary = client.get("/security/summary", headers=headers)
        assert summary.status_code == 200
        payload = summary.json()["summary"]
        assert payload["cloud_calls"] == 1
        assert payload["by_action"][sa.CLOUD_CALL] == 1
    finally:
        api._API_TOKEN = old_token


def test_event_bus_threat_block_emits_security_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SECURITY_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    from infra.event_bus import app
    from infra import security_audit as sa

    client = TestClient(app)
    fake_key = "sk-ant-" + ("a" * 48)
    resp = client.post(
        "/tasks",
        json={
            "title": "Rotate credential",
            "description": f"Use {fake_key} for this task",
            "agent": "backend_engineer",
        },
    )

    assert resp.status_code == 202
    assert resp.json()["status"] == "waiting_approval"
    events = sa.read_events()
    assert events
    assert events[0]["action"] == sa.THREAT_SCREEN_BLOCK
    assert events[0]["decision"] == "held"


def test_security_reviewer_verdict_emits_security_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SECURITY_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    from agents import security_reviewer as sr
    from infra import security_audit as sa

    raw = json.dumps({
        "verdict": "PASS",
        "severity": "none",
        "findings": [],
        "summary": "No issues found.",
    })
    with patch("agent_dispatch.dispatch", return_value=iter([raw])):
        verdict = sr.review({"task": "read local docs"}, task_id="task-audit-1", stage=2)

    assert verdict.verdict == "PASS"
    events = sa.read_events(action=sa.SECURITY_VERDICT)
    assert len(events) == 1
    assert events[0]["task_id"] == "task-audit-1"
    assert events[0]["decision"] == "PASS"


def test_hackingtool_blocked_run_emits_security_tool_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SECURITY_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setitem(sys.modules, "infra.memory", types.SimpleNamespace(store=None))

    from infra import security_audit as sa
    from tools.security.hackingtool_adapter import ToolRunRequest, run_tool

    result = run_tool(ToolRunRequest(
        tool="nmap",
        args=["-sV"],
        target="127.0.0.1",
        agent_id="security_reviewer",
    ))

    assert result.blocked is True
    events = sa.read_events(action=sa.SECURITY_TOOL_EXEC)
    assert len(events) == 1
    assert events[0]["decision"] == "blocked"
    assert "requires explicit human approval" in events[0]["reason"]


def test_security_audit_jsonl_contains_no_unparseable_lines(tmp_path, monkeypatch):
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("JARVIS_SECURITY_AUDIT_PATH", str(audit_path))

    from infra import security_audit as sa

    sa.audit_event(sa.RBAC_DENY, actor="agent", target="tool", decision="deny")
    lines = audit_path.read_text().splitlines()
    assert lines
    for line in lines:
        assert isinstance(json.loads(line), dict)
