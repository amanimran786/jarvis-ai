"""Unit tests for google_services circuit breaker (_cb_check, _cb_record_failure, _cb_record_success).

Tests are hermetic: they manipulate _cb_state and _CB_OPEN_TTL directly
without touching Google APIs or credentials.
"""
from __future__ import annotations

import time

import google_services as gs


def _reset(tool: str) -> None:
    """Clear circuit breaker state for one tool."""
    with gs._CB_LOCK:
        gs._cb_state.pop(tool, None)


class TestCbCheck:
    def setup_method(self):
        _reset("calendar")
        _reset("gmail")

    def test_closed_by_default(self):
        assert gs._cb_check("calendar") is False

    def test_open_when_past_deadline(self):
        with gs._CB_LOCK:
            gs._cb_state["calendar"] = {"fails": [], "open_until": time.monotonic() - 1}
        assert gs._cb_check("calendar") is False

    def test_open_when_future_deadline(self):
        with gs._CB_LOCK:
            gs._cb_state["calendar"] = {"fails": [], "open_until": time.monotonic() + 9999}
        assert gs._cb_check("calendar") is True


class TestCbRecordFailure:
    def setup_method(self):
        _reset("calendar")

    def test_below_threshold_stays_closed(self):
        for _ in range(gs._CB_THRESHOLD - 1):
            gs._cb_record_failure("calendar")
        assert gs._cb_check("calendar") is False

    def test_at_threshold_opens_circuit(self):
        for _ in range(gs._CB_THRESHOLD):
            gs._cb_record_failure("calendar")
        assert gs._cb_check("calendar") is True

    def test_open_until_set_to_future(self):
        for _ in range(gs._CB_THRESHOLD):
            gs._cb_record_failure("calendar")
        now = time.monotonic()
        with gs._CB_LOCK:
            open_until = gs._cb_state["calendar"]["open_until"]
        assert open_until > now

    def test_old_failures_outside_window_ignored(self):
        now = time.monotonic()
        with gs._CB_LOCK:
            gs._cb_state["calendar"] = {
                "fails": [now - gs._CB_WINDOW - 1] * (gs._CB_THRESHOLD - 1),
                "open_until": 0.0,
            }
        # One new failure should NOT open circuit (old ones are pruned)
        gs._cb_record_failure("calendar")
        assert gs._cb_check("calendar") is False


class TestCbRecordSuccess:
    def setup_method(self):
        _reset("gmail")

    def test_success_clears_fail_count(self):
        # Record failures up to threshold - 1, then a success resets
        for _ in range(gs._CB_THRESHOLD - 1):
            gs._cb_record_failure("gmail")
        gs._cb_record_success("gmail")
        with gs._CB_LOCK:
            fails = gs._cb_state.get("gmail", {}).get("fails", [])
        assert fails == []

    def test_success_on_unknown_tool_is_noop(self):
        # Should not raise or create state
        gs._cb_record_success("nonexistent_tool_xyz")
        with gs._CB_LOCK:
            assert "nonexistent_tool_xyz" not in gs._cb_state

    def test_success_does_not_close_already_open_circuit(self):
        # Circuit opened by 3 failures; success clears fail list but does NOT
        # reset open_until — tool stays blocked until TTL expires
        for _ in range(gs._CB_THRESHOLD):
            gs._cb_record_failure("gmail")
        assert gs._cb_check("gmail") is True
        gs._cb_record_success("gmail")
        # open_until is still in the future
        assert gs._cb_check("gmail") is True
