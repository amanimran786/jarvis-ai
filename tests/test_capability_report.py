from __future__ import annotations

import capability_report


def _health(**overrides: bool) -> dict[str, dict[str, object]]:
    states = {
        "ollama": True,
        "stt": True,
        "tts": True,
        "vault": True,
        "mem0": True,
        "google": True,
        "watcher": True,
    }
    states.update(overrides)
    return {
        name: {
            "name": name,
            "ok": ready,
            "degraded": not ready,
            "detail": "Watcher active" if name == "watcher" else "",
        }
        for name, ready in states.items()
    }


def test_capabilities_reply_separates_scope_from_live_readiness() -> None:
    report = capability_report.capabilities_reply(
        health=_health(google=False, mem0=False),
        message_history={"ok": False},
    )

    assert "Implemented capabilities" in report
    assert "Agentic execution" in report
    assert "Python code loop" in report
    assert "generate code in an isolated workspace" in report
    assert "Current readiness" in report
    assert "Local models: ready" in report
    assert "Semantic/episodic memory: needs attention" in report
    assert "Calendar and Gmail: needs attention" in report
    assert "iMessage: draft/send is confirmation-gated" in report
    assert "training and promotion are never silent" in report


def test_capabilities_reply_does_not_invent_unprobed_readiness() -> None:
    report = capability_report.capabilities_reply(health={}, message_history={})

    assert "Local models: not verified" in report
    assert "Calendar and Gmail: not verified" in report
    assert "history is not verified" in report
    assert "Proactive alerts: not verified" in report


def test_capabilities_reply_reports_disabled_watcher() -> None:
    health = _health()
    health["watcher"]["detail"] = "Proactive watcher disabled"

    report = capability_report.capabilities_reply(
        health=health,
        message_history={"ok": True},
    )

    assert "iMessage: draft/send is confirmation-gated; history is ready" in report
    assert "Proactive alerts: disabled" in report


def test_mobile_guidance_forbids_unverified_integration_claims() -> None:
    guidance = capability_report.mobile_system_guidance()

    assert "Never claim" in guidance
    assert "unless runtime evidence verifies them" in guidance
    assert "approval-gated rather than autonomous" in guidance


def test_router_capability_fast_path_uses_shared_report(monkeypatch) -> None:
    import router

    monkeypatch.setattr(capability_report, "capabilities_reply", lambda: "runtime-aware report")

    stream, label = router.route_stream("what can you do now?")

    assert "".join(stream) == "runtime-aware report"
    assert label == "Status"
