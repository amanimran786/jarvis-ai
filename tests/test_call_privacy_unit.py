"""Unit tests for call_privacy.py public API.

call_privacy keeps all state in module-level globals (`_enabled`,
`_cache_until`, `_cached_meeting`). These tests reset every one of them
before each test so the cases stay independent — resetting `_enabled`
alone is insufficient because the meeting-label cache persists across
calls and would otherwise leak an "active meeting" into later tests.

The meeting label is normally produced by `_meeting_label()`, which
lazily imports `desktop.overlay` and falls back to "NONE" on any
exception. We never mock that import; instead we drive the time-based
cache directly: priming `_cached_meeting`/`_cache_until` simulates an
active meeting deterministically without depending on `desktop` existing.
"""

import time

import pytest

import call_privacy


@pytest.fixture(autouse=True)
def reset_call_privacy_state():
    """Reset every module global to a known default-ON / no-meeting state."""
    call_privacy.set_enabled(True)
    call_privacy._cache_until = 0.0
    call_privacy._cached_meeting = "NONE"
    yield
    call_privacy.set_enabled(True)
    call_privacy._cache_until = 0.0
    call_privacy._cached_meeting = "NONE"


def _prime_meeting(label: str = "Zoom") -> None:
    """Force `_meeting_label()` to return `label` via the time-based cache."""
    call_privacy._cached_meeting = label
    call_privacy._cache_until = time.monotonic() + 60


# --- default state ---------------------------------------------------------


def test_default_enabled_is_true():
    assert call_privacy.is_enabled() is True


# --- set_enabled() ---------------------------------------------------------


def test_set_enabled_false_disables():
    assert call_privacy.set_enabled(False) is False
    assert call_privacy.is_enabled() is False


def test_set_enabled_true_enables():
    call_privacy.set_enabled(False)
    assert call_privacy.set_enabled(True) is True
    assert call_privacy.is_enabled() is True


def test_set_enabled_coerces_truthy():
    assert call_privacy.set_enabled(1) is True
    assert call_privacy.set_enabled("x") is True
    assert call_privacy.is_enabled() is True


def test_set_enabled_coerces_falsy():
    assert call_privacy.set_enabled(0) is False
    assert call_privacy.set_enabled("") is False
    assert call_privacy.is_enabled() is False


# --- toggle_enabled() ------------------------------------------------------


def test_toggle_flips_true_to_false():
    call_privacy.set_enabled(True)
    assert call_privacy.toggle_enabled() is False
    assert call_privacy.is_enabled() is False


def test_toggle_flips_false_to_true():
    call_privacy.set_enabled(False)
    assert call_privacy.toggle_enabled() is True
    assert call_privacy.is_enabled() is True


def test_toggle_twice_returns_to_start():
    call_privacy.set_enabled(True)
    call_privacy.toggle_enabled()
    assert call_privacy.toggle_enabled() is True
    assert call_privacy.is_enabled() is True


# --- should_suppress_audio() ----------------------------------------------


def test_no_suppress_when_disabled():
    call_privacy.set_enabled(False)
    assert call_privacy.should_suppress_audio() is False


def test_no_suppress_when_no_meeting():
    call_privacy.set_enabled(True)
    assert call_privacy.should_suppress_audio() is False


def test_suppress_when_enabled_and_meeting():
    call_privacy.set_enabled(True)
    _prime_meeting("Zoom")
    assert call_privacy.should_suppress_audio() is True


def test_no_suppress_when_meeting_but_disabled():
    call_privacy.set_enabled(False)
    _prime_meeting("Zoom")
    assert call_privacy.should_suppress_audio() is False


# --- snapshot() ------------------------------------------------------------


def test_snapshot_keys():
    assert set(call_privacy.snapshot().keys()) == {
        "enabled",
        "meeting",
        "suppressing_audio",
    }


def test_snapshot_reflects_enabled_no_meeting():
    call_privacy.set_enabled(True)
    state = call_privacy.snapshot()
    assert state["enabled"] is True
    assert state["meeting"] == "NONE"
    assert state["suppressing_audio"] is False


def test_snapshot_reflects_disabled():
    call_privacy.set_enabled(False)
    state = call_privacy.snapshot()
    assert state["enabled"] is False
    assert state["suppressing_audio"] is False


def test_snapshot_reflects_suppressing():
    call_privacy.set_enabled(True)
    _prime_meeting("Zoom")
    state = call_privacy.snapshot()
    assert state["enabled"] is True
    assert state["meeting"] == "Zoom"
    assert state["suppressing_audio"] is True


# --- status_text() ---------------------------------------------------------


def test_status_text_off():
    call_privacy.set_enabled(False)
    text = call_privacy.status_text()
    assert "OFF" in text


def test_status_text_on_no_meeting():
    call_privacy.set_enabled(True)
    text = call_privacy.status_text()
    assert "ON" in text
    assert "automatically" in text


def test_status_text_suppressing():
    call_privacy.set_enabled(True)
    _prime_meeting("Zoom")
    text = call_privacy.status_text()
    assert "Zoom" in text
    assert "suppressed" in text
