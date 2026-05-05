"""Tests for the live beta feedback recorder."""

from __future__ import annotations

import json

from local_runtime import live_beta


def _reset_state(*, enabled: bool = True, autorun: bool = False) -> None:
    live_beta._STATE.update(
        {
            "enabled": enabled,
            "autorun": autorun,
            "train_pack": False,
            "last_run": 0.0,
            "running": False,
            "events": 0,
        }
    )


def test_record_interaction_writes_feedback_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(live_beta.local_beta, "ROOT", tmp_path)
    _reset_state(enabled=True)

    live_beta.record_interaction(
        {
            "id": "abc123",
            "user_input": "hello",
            "response": "Hi Aman.",
            "model": "Open-Source",
        },
        source="test",
    )

    events = (tmp_path / "live_feedback.jsonl").read_text(encoding="utf-8").splitlines()
    payload = json.loads(events[0])
    assert payload["event"] == "interaction"
    assert payload["source"] == "test"
    assert payload["interaction_id"] == "abc123"
    assert live_beta.status()["events"] == 1


def test_record_interaction_respects_disabled_state(tmp_path, monkeypatch):
    monkeypatch.setattr(live_beta.local_beta, "ROOT", tmp_path)
    _reset_state(enabled=False)

    live_beta.record_interaction({"id": "abc123"}, source="test")

    assert not (tmp_path / "live_feedback.jsonl").exists()
    assert live_beta.status()["events"] == 0
