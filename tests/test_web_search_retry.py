"""
tests/test_web_search_retry.py — 15 tests for retry, cache, and audit in harness/web_search.py

Covers:
  1. Retry on rate limit (429 message, 'ratelimit' keyword)
  2. Backoff timing (mock sleep) → 1s, 2s, 4s
  3. Cache hit within 5 minutes
  4. Cache miss after 5 minutes (TTL expired)
  5. Cache key normalisation (whitespace / case)
  6. Timeout retry (one immediate retry, no sleep)
  7. Max retries exceeded → RuntimeError propagated as "Search failed" string
  8. Audit log called on each retry attempt
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from harness import web_search as ws


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fake_results(n: int = 3) -> list[dict]:
    return [
        {"title": f"R{i}", "body": "x" * 50, "href": f"https://ex.com/{i}"}
        for i in range(1, n + 1)
    ]


def _make_ddgs_class(side_effects: list):
    """Return a DDGS-compatible class whose .text() applies side_effects in order."""
    call_count = [0]

    class MockDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def text(self, query, max_results=5):
            idx = call_count[0]
            call_count[0] += 1
            effect = side_effects[idx] if idx < len(side_effects) else side_effects[-1]
            if isinstance(effect, BaseException):
                raise effect
            return iter(effect)

    MockDDGS._call_count = call_count   # expose for assertions
    return MockDDGS


_RATE_LIMIT_EXC = Exception("HTTP 429 Too Many Requests")
_TIMEOUT_EXC    = Exception("connection timed out")


# ── 1. No retry on clean success ──────────────────────────────────────────────

class TestNoRetryOnSuccess:
    def setup_method(self):
        ws._query_cache.clear()

    def test_success_ddgs_called_exactly_once(self):
        cls = _make_ddgs_class([_fake_results()])
        with patch("harness.web_search.DDGS", cls), \
             patch("harness.web_search._summarise", return_value=""):
            out = ws.search("python", summarise=False)
        assert "[1]" in out
        assert cls._call_count[0] == 1


# ── 2. Rate-limit retry ───────────────────────────────────────────────────────

class TestRateLimitRetry:
    def setup_method(self):
        ws._query_cache.clear()

    def test_retry_on_429_message(self):
        """HTTP 429 in message → retries."""
        cls = _make_ddgs_class([_RATE_LIMIT_EXC, _fake_results()])
        with patch("harness.web_search.DDGS", cls), \
             patch("harness.web_search.time.sleep"), \
             patch("harness.web_search._log_retry"), \
             patch("harness.web_search._summarise", return_value=""):
            out = ws.search("retry-ok", summarise=False)
        assert "[1]" in out
        assert cls._call_count[0] == 2

    def test_retry_on_ratelimit_keyword(self):
        """'ratelimit' in message → retries."""
        cls = _make_ddgs_class([Exception("ratelimit exceeded"), _fake_results()])
        with patch("harness.web_search.DDGS", cls), \
             patch("harness.web_search.time.sleep"), \
             patch("harness.web_search._log_retry"), \
             patch("harness.web_search._summarise", return_value=""):
            out = ws.search("ratelimit-key", summarise=False)
        assert "[1]" in out

    def test_backoff_sleeps_1s_on_first_retry(self):
        sleeps: list[float] = []
        cls = _make_ddgs_class([_RATE_LIMIT_EXC, _fake_results()])
        with patch("harness.web_search.DDGS", cls), \
             patch("harness.web_search.time.sleep", side_effect=sleeps.append), \
             patch("harness.web_search._log_retry"), \
             patch("harness.web_search._summarise", return_value=""):
            ws.search("backoff-1", summarise=False)
        assert sleeps == [1.0]

    def test_backoff_three_retries_sleeps_1_2_4(self):
        """Three rate limits → sleeps 1 s, 2 s, 4 s (exponential)."""
        sleeps: list[float] = []
        cls = _make_ddgs_class([
            _RATE_LIMIT_EXC, _RATE_LIMIT_EXC, _RATE_LIMIT_EXC, _fake_results()
        ])
        with patch("harness.web_search.DDGS", cls), \
             patch("harness.web_search.time.sleep", side_effect=sleeps.append), \
             patch("harness.web_search._log_retry"), \
             patch("harness.web_search._summarise", return_value=""):
            out = ws.search("backoff-all", summarise=False)
        assert sleeps == [1.0, 2.0, 4.0]
        assert "[1]" in out

    def test_max_retries_exceeded_returns_search_failed(self):
        """4 consecutive 429s → exhausted → returns 'Search failed'."""
        cls = _make_ddgs_class([_RATE_LIMIT_EXC] * 10)
        with patch("harness.web_search.DDGS", cls), \
             patch("harness.web_search.time.sleep"), \
             patch("harness.web_search._log_retry"):
            out = ws.search("max-fail")
        assert "Search failed" in out

    def test_max_retries_total_calls_is_4(self):
        """Initial attempt + 3 retries = exactly 4 DDGS calls."""
        cls = _make_ddgs_class([_RATE_LIMIT_EXC] * 10)
        with patch("harness.web_search.DDGS", cls), \
             patch("harness.web_search.time.sleep"), \
             patch("harness.web_search._log_retry"):
            ws.search("count-calls")
        assert cls._call_count[0] == 4   # 1 initial + 3 retries

    def test_ddgs_search_raises_runtime_error_after_max(self):
        """_ddgs_search() itself raises RuntimeError after exhausting retries."""
        cls = _make_ddgs_class([_RATE_LIMIT_EXC] * 10)
        with pytest.raises(RuntimeError, match="retries"):
            with patch("harness.web_search.DDGS", cls), \
                 patch("harness.web_search.time.sleep"), \
                 patch("harness.web_search._log_retry"):
                ws._ddgs_search("q", 5)


# ── 3. Timeout retry ──────────────────────────────────────────────────────────

class TestTimeoutRetry:
    def setup_method(self):
        ws._query_cache.clear()

    def test_timeout_retried_once_and_succeeds(self):
        cls = _make_ddgs_class([_TIMEOUT_EXC, _fake_results()])
        with patch("harness.web_search.DDGS", cls), \
             patch("harness.web_search._log_retry"), \
             patch("harness.web_search._summarise", return_value=""):
            out = ws.search("timeout-ok", summarise=False)
        assert "[1]" in out
        assert cls._call_count[0] == 2

    def test_timeout_retry_no_sleep(self):
        """Timeout retry is immediate — time.sleep must NOT be called."""
        cls = _make_ddgs_class([_TIMEOUT_EXC, _fake_results()])
        with patch("harness.web_search.DDGS", cls), \
             patch("harness.web_search.time.sleep") as mock_sleep, \
             patch("harness.web_search._log_retry"), \
             patch("harness.web_search._summarise", return_value=""):
            ws.search("timeout-nosleep", summarise=False)
        mock_sleep.assert_not_called()


# ── 4. Cache ──────────────────────────────────────────────────────────────────

class TestCache:
    def setup_method(self):
        ws._query_cache.clear()

    def test_cache_hit_within_5_min_skips_ddgs(self):
        cls = _make_ddgs_class([_fake_results()])
        with patch("harness.web_search.DDGS", cls), \
             patch("harness.web_search._summarise", return_value=""):
            ws.search("cached q", summarise=False)
            ws.search("cached q", summarise=False)
        assert cls._call_count[0] == 1  # only one real call

    def test_cache_miss_after_ttl_expired(self):
        tick = [0.0]

        def _mono():
            return tick[0]

        cls = _make_ddgs_class([_fake_results(), _fake_results()])
        with patch("harness.web_search.DDGS", cls), \
             patch("harness.web_search.time.monotonic", side_effect=_mono), \
             patch("harness.web_search._summarise", return_value=""):
            ws.search("expire me", summarise=False)
            tick[0] = ws._CACHE_TTL + 1.0
            ws.search("expire me", summarise=False)
        assert cls._call_count[0] == 2  # TTL expired → re-fetched

    def test_cache_key_normalisation_case(self):
        """'Python' and 'python' share the same cache entry."""
        cls = _make_ddgs_class([_fake_results()])
        with patch("harness.web_search.DDGS", cls), \
             patch("harness.web_search._summarise", return_value=""):
            ws.search("Python",  summarise=False)
            ws.search("python",  summarise=False)
        assert cls._call_count[0] == 1

    def test_cache_key_normalisation_whitespace(self):
        """'python   tutorial' normalises to 'python tutorial'."""
        cls = _make_ddgs_class([_fake_results()])
        with patch("harness.web_search.DDGS", cls), \
             patch("harness.web_search._summarise", return_value=""):
            ws.search("python   tutorial", summarise=False)
            ws.search("python tutorial",   summarise=False)
        assert cls._call_count[0] == 1

    def test_failed_search_not_cached(self):
        """A search error does not populate the cache."""
        cls = _make_ddgs_class([_RATE_LIMIT_EXC] * 10)
        with patch("harness.web_search.DDGS", cls), \
             patch("harness.web_search.time.sleep"), \
             patch("harness.web_search._log_retry"):
            ws.search("fail-cache")
        key = (ws._normalize_query("fail-cache"), 5)
        assert key not in ws._query_cache


# ── 5. Audit logging ──────────────────────────────────────────────────────────

class TestAuditLogging:
    def setup_method(self):
        ws._query_cache.clear()

    def test_log_retry_called_on_rate_limit(self):
        """_log_retry fired with reason='rate_limit'."""
        cls = _make_ddgs_class([_RATE_LIMIT_EXC, _fake_results()])
        with patch("harness.web_search.DDGS", cls), \
             patch("harness.web_search.time.sleep"), \
             patch("harness.web_search._summarise", return_value=""), \
             patch("harness.web_search._log_retry") as mock_log:
            ws.search("audit-rate", summarise=False)
        reasons = [c.args[2] for c in mock_log.call_args_list]
        assert "rate_limit" in reasons

    def test_log_retry_called_on_timeout(self):
        """_log_retry fired with reason='timeout'."""
        cls = _make_ddgs_class([_TIMEOUT_EXC, _fake_results()])
        with patch("harness.web_search.DDGS", cls), \
             patch("harness.web_search._summarise", return_value=""), \
             patch("harness.web_search._log_retry") as mock_log:
            ws.search("audit-timeout", summarise=False)
        reasons = [c.args[2] for c in mock_log.call_args_list]
        assert "timeout" in reasons

    def test_no_log_retry_on_clean_success(self):
        """_log_retry NOT called when first attempt succeeds."""
        cls = _make_ddgs_class([_fake_results()])
        with patch("harness.web_search.DDGS", cls), \
             patch("harness.web_search._summarise", return_value=""), \
             patch("harness.web_search._log_retry") as mock_log:
            ws.search("clean", summarise=False)
        mock_log.assert_not_called()
