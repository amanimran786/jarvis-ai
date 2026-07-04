"""Rate-limit retry/backoff behavior of brains._retry.

Covers: 429 detection heuristics, exponential 2s/4s/8s schedule, max-3-retries
bound (fail fast so the router falls through to the next provider), and the
no-retry path for non-rate-limit errors.
"""

import pytest
from unittest.mock import patch

from brains import _retry


class _FakeRateLimitError(Exception):
    pass


class _Fake429WithStatus(Exception):
    status_code = 429


def test_is_rate_limit_error_detects_common_shapes():
    assert _retry.is_rate_limit_error(Exception("HTTP 429 Too Many Requests"))
    assert _retry.is_rate_limit_error(Exception("rate limit exceeded"))
    assert _retry.is_rate_limit_error(Exception("RESOURCE_EXHAUSTED: quota"))
    assert _retry.is_rate_limit_error(Exception("model overloaded"))
    assert _retry.is_rate_limit_error(_Fake429WithStatus("slow down"))
    # Exception class name alone matches (e.g. openai.RateLimitError)
    assert _retry.is_rate_limit_error(_FakeRateLimitError("x"))
    assert not _retry.is_rate_limit_error(Exception("connection reset by peer"))
    assert not _retry.is_rate_limit_error(ValueError("bad json"))


def test_backoff_schedule_is_2_4_8():
    assert _retry.backoff_delay(0) == 2.0
    assert _retry.backoff_delay(1) == 4.0
    assert _retry.backoff_delay(2) == 8.0


def test_call_with_backoff_retries_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Exception("429 too many requests")
        return "ok"

    with patch.object(_retry.time, "sleep") as mock_sleep:
        assert _retry.call_with_backoff(flaky, provider="test") == "ok"
    assert calls["n"] == 3
    assert [c.args[0] for c in mock_sleep.call_args_list] == [2.0, 4.0]


def test_call_with_backoff_raises_after_max_retries():
    calls = {"n": 0}

    def always_limited():
        calls["n"] += 1
        raise Exception("rate limit exceeded")

    with patch.object(_retry.time, "sleep") as mock_sleep:
        with pytest.raises(Exception, match="rate limit"):
            _retry.call_with_backoff(always_limited, provider="test")
    # 1 initial attempt + 3 retries, sleeps 2s, 4s, 8s
    assert calls["n"] == 4
    assert [c.args[0] for c in mock_sleep.call_args_list] == [2.0, 4.0, 8.0]


def test_call_with_backoff_does_not_retry_other_errors():
    calls = {"n": 0}

    def broken():
        calls["n"] += 1
        raise ValueError("bad request")

    with patch.object(_retry.time, "sleep") as mock_sleep:
        with pytest.raises(ValueError):
            _retry.call_with_backoff(broken, provider="test")
    assert calls["n"] == 1
    mock_sleep.assert_not_called()
