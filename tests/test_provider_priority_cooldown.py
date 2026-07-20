"""Rate-limit cooldown behavior in provider_priority.ask_with_priority."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import provider_priority as pp


@pytest.fixture(autouse=True)
def _clean_cooldowns(tmp_path, monkeypatch):
    # Isolate the persistent circuit breaker so these tests never touch the
    # real logs/circuit_breaker.json or ORCHESTRATOR_STATUS.json.
    monkeypatch.setenv("JARVIS_CIRCUIT_BREAKER_PATH", str(tmp_path / "circuit_breaker.json"))
    monkeypatch.setenv("JARVIS_ORCHESTRATOR_STATUS_PATH", str(tmp_path / "ORCHESTRATOR_STATUS.json"))
    from harness import circuit_breaker
    circuit_breaker.reset()
    pp._provider_cooldowns.clear()
    yield
    circuit_breaker.reset()
    pp._provider_cooldowns.clear()


def _cloud_only():
    """Patch context: local lane disabled so the cloud plan runs directly."""
    return patch.multiple(
        pp,
        FREE_FIRST_ENABLED=False,
        _open_source_mode=lambda: False,
    )


def _quiet_teacher():
    return patch.object(pp._teacher_capture, "capture", lambda *a, **kw: None)


def test_rate_limit_error_starts_cooldown_and_falls_through():
    with _cloud_only(), _quiet_teacher(), \
         patch.object(pp, "_ask_openai", side_effect=RuntimeError("429 Too Many Requests")), \
         patch.object(pp, "_ask_gemini", return_value="gemini answer"):
        answer = pp.ask_with_priority("q", tier="cheap")

    assert answer == "gemini answer"
    assert pp._in_cooldown("openai")
    assert not pp._in_cooldown("gemini")


def test_provider_in_cooldown_is_skipped_without_being_called():
    calls = []
    with _cloud_only(), _quiet_teacher(), \
         patch.object(pp, "_ask_openai", side_effect=lambda *a, **kw: calls.append("openai")), \
         patch.object(pp, "_ask_gemini", return_value="gemini answer"):
        pp._start_cooldown("openai")
        answer = pp.ask_with_priority("q", tier="cheap")

    assert answer == "gemini answer"
    assert calls == []  # openai runner never invoked


def test_non_rate_limit_error_does_not_start_cooldown():
    with _cloud_only(), _quiet_teacher(), \
         patch.object(pp, "_ask_openai", side_effect=RuntimeError("connection reset")), \
         patch.object(pp, "_ask_gemini", return_value="ok"):
        pp.ask_with_priority("q", tier="cheap")

    assert not pp._in_cooldown("openai")


def test_cooldown_expires():
    fake_now = [1000.0]
    with patch.object(pp.time, "monotonic", side_effect=lambda: fake_now[0]), \
         patch.dict("os.environ", {"JARVIS_PROVIDER_COOLDOWN_SECONDS": "60"}):
        pp._start_cooldown("openai")
        assert pp._in_cooldown("openai")
        fake_now[0] += 61.0
        assert not pp._in_cooldown("openai")


def test_all_providers_cooling_down_raises():
    with _cloud_only(), _quiet_teacher():
        for provider in ("openai", "gemini", "anthropic"):
            pp._start_cooldown(provider)
        with pytest.raises(RuntimeError, match="cooldown"):
            pp.ask_with_priority("q", tier="cheap")


@pytest.mark.parametrize(
    "message,expected",
    [
        ("429 Too Many Requests", True),
        ("rate limit exceeded", True),
        ("RESOURCE_EXHAUSTED: quota exceeded", True),
        ("Overloaded", True),
        ("connection refused", False),
        ("invalid api key", False),
    ],
)
def test_is_rate_limit_error_markers(message, expected):
    assert pp._is_rate_limit_error(RuntimeError(message)) is expected


def test_rate_limit_error_detected_by_exception_class_name():
    class RateLimitError(Exception):
        pass

    assert pp._is_rate_limit_error(RateLimitError("try later"))
