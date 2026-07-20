"""Persistent circuit breaker state machine and snapshot behavior."""

import json
from unittest.mock import patch

import pytest

from harness import circuit_breaker as cb


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CIRCUIT_BREAKER_PATH", str(tmp_path / "circuit_breaker.json"))
    monkeypatch.setenv(
        "JARVIS_ORCHESTRATOR_STATUS_PATH", str(tmp_path / "ORCHESTRATOR_STATUS.json")
    )
    cb.reset()
    yield tmp_path
    cb.reset()


def test_starts_closed_and_available():
    assert cb.is_available("openai")
    assert cb.get_state("openai")["state"] == cb.CLOSED


def test_opens_after_three_consecutive_failures():
    for _ in range(2):
        cb.record_failure("openai")
    assert cb.is_available("openai")
    cb.record_failure("openai")
    assert not cb.is_available("openai")
    assert cb.get_state("openai")["state"] == cb.OPEN


def test_success_resets_failures():
    cb.record_failure("openai")
    cb.record_failure("openai")
    cb.record_success("openai")
    state = cb.get_state("openai")
    assert state["state"] == cb.CLOSED
    assert state["failures"] == 0


def test_half_open_after_window_then_closes_on_success():
    fake_now = [1000.0]
    with patch.object(cb.time, "time", side_effect=lambda: fake_now[0]):
        for _ in range(3):
            cb.record_failure("gemini")
        assert not cb.is_available("gemini")
        fake_now[0] += cb.OPEN_SECONDS + 1
        assert cb.is_available("gemini")  # HALF_OPEN probe allowed
        assert cb.get_state("gemini")["state"] == cb.HALF_OPEN
        cb.record_success("gemini")
        assert cb.get_state("gemini")["state"] == cb.CLOSED


def test_half_open_failure_reopens():
    fake_now = [1000.0]
    with patch.object(cb.time, "time", side_effect=lambda: fake_now[0]):
        for _ in range(3):
            cb.record_failure("gemini")
        fake_now[0] += cb.OPEN_SECONDS + 1
        assert cb.is_available("gemini")
        cb.record_failure("gemini")
        assert not cb.is_available("gemini")
        assert cb.get_state("gemini")["state"] == cb.OPEN


def test_state_persists_across_reload(tmp_path):
    for _ in range(3):
        cb.record_failure("anthropic")
    # Simulate process restart: drop the in-memory cache and reload from disk.
    with cb._lock:
        cb._states = None
    assert not cb.is_available("anthropic")


def test_status_snapshot_written(_isolated_state):
    cb.record_failure("openai")
    status_path = _isolated_state / "ORCHESTRATOR_STATUS.json"
    assert status_path.is_file()
    data = json.loads(status_path.read_text())
    assert data["provider_health"]["openai"]["failures"] == 1
    assert data["provider_health"]["openai"]["state"] == cb.CLOSED


def test_status_snapshot_preserves_existing_keys(_isolated_state):
    status_path = _isolated_state / "ORCHESTRATOR_STATUS.json"
    status_path.write_text(json.dumps({"sessions": [{"name": "x"}]}))
    cb.record_failure("openai")
    data = json.loads(status_path.read_text())
    assert data["sessions"] == [{"name": "x"}]
    assert "provider_health" in data


def test_open_snapshot_has_until_field(_isolated_state):
    for _ in range(3):
        cb.record_failure("openai")
    data = json.loads((_isolated_state / "ORCHESTRATOR_STATUS.json").read_text())
    assert data["provider_health"]["openai"]["state"] == cb.OPEN
    assert data["provider_health"]["openai"]["until"]


@pytest.mark.parametrize(
    "message,expected",
    [
        ("429 Too Many Requests", True),
        ("RESOURCE_EXHAUSTED: quota exceeded", True),
        ("connection refused", False),
    ],
)
def test_is_rate_limit_error(message, expected):
    assert cb.is_rate_limit_error(RuntimeError(message)) is expected
