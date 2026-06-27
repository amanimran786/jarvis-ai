"""Tests for tools.web_search, tools.fetch_page, tools.web_search_with_fetch.

All network calls are mocked — no real HTTP or Ollama calls during test runs.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call
import urllib.error

import tools


# ── helpers ───────────────────────────────────────────────────────────────────

def _ddg_results(n: int = 3) -> list[dict]:
    return [
        {
            "title": f"Result {i}",
            "body": f"This is a longer snippet for result {i} with enough text to exceed the 300-char threshold.",
            "href": f"https://example.com/{i}",
        }
        for i in range(1, n + 1)
    ]


def _mock_ddgs(results):
    """Context manager mock for DDGS().text()."""
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.text = MagicMock(return_value=iter(results))
    return ctx


# ── web_search ────────────────────────────────────────────────────────────────

class TestWebSearch:
    def test_returns_titles_and_urls(self):
        with patch("tools.DDGS", return_value=_mock_ddgs(_ddg_results(2))), \
             patch("tools._summarise_for_voice", return_value="summary"):
            out = tools.web_search("python asyncio", summarise=False)
        assert "Result 1" in out
        assert "https://example.com/1" in out

    def test_no_results_returns_not_found(self):
        with patch("tools.DDGS", return_value=_mock_ddgs([])):
            out = tools.web_search("xyzzy12345")
        assert "couldn't find" in out.lower() or "nothing" in out.lower()

    def test_ddgs_exception_returns_error_string(self):
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(side_effect=RuntimeError("rate limited"))
        ctx.__exit__ = MagicMock(return_value=False)
        with patch("tools.DDGS", return_value=ctx):
            out = tools.web_search("query")
        assert "Search failed" in out or "rate limited" in out

    def test_summarise_called_when_raw_is_long(self):
        with patch("tools.DDGS", return_value=_mock_ddgs(_ddg_results(5))), \
             patch("tools._summarise_for_voice", return_value="summarised") as mock_sum:
            out = tools.web_search("query", summarise=True)
        mock_sum.assert_called_once()
        assert out == "summarised"

    def test_summarise_skipped_when_disabled(self):
        with patch("tools.DDGS", return_value=_mock_ddgs(_ddg_results(5))), \
             patch("tools._summarise_for_voice") as mock_sum:
            tools.web_search("query", summarise=False)
        mock_sum.assert_not_called()

    def test_summarise_timeout_falls_back_to_raw(self):
        import time
        def _slow_sum(raw, query):
            time.sleep(30)  # will be cancelled by thread.join(timeout=12)
            return "should not appear"

        with patch("tools.DDGS", return_value=_mock_ddgs(_ddg_results(5))), \
             patch("tools._summarise_for_voice", side_effect=_slow_sum):
            # patch threading.Thread.join to return immediately (simulates timeout)
            import threading
            orig_join = threading.Thread.join
            def _fast_join(self, timeout=None):
                return  # return without waiting — result_holder stays empty
            with patch.object(threading.Thread, "join", _fast_join):
                out = tools.web_search("query", summarise=True)
        # fell back to raw
        assert "Result 1" in out


# ── _summarise_for_voice ─────────────────────────────────────────────────────

class TestSummariseForVoice:
    def test_uses_get_best_available_not_hardcoded_model(self):
        summary = "Nice summary of search results with enough characters to pass the guard."
        with patch("brains.brain_ollama.get_best_available", return_value="glm-4v-flash") as mock_gba, \
             patch("brains.brain_ollama.ask_local", return_value=summary):
            result = tools._summarise_for_voice("raw results text here", "python")
        mock_gba.assert_called_once()
        assert result == summary

    def test_falls_back_to_raw_on_exception(self):
        with patch("brains.brain_ollama.get_best_available", side_effect=RuntimeError("Ollama offline")):
            result = tools._summarise_for_voice("raw result text", "query")
        assert result == "raw result text"

    def test_falls_back_to_raw_when_summary_too_short(self):
        with patch("brains.brain_ollama.get_best_available", return_value="model"), \
             patch("brains.brain_ollama.ask_local", return_value="ok"):  # len <= 20
            result = tools._summarise_for_voice("raw result text that is long enough", "q")
        assert result == "raw result text that is long enough"


# ── fetch_page ────────────────────────────────────────────────────────────────

class TestFetchPage:
    def _mock_urlopen(self, content: bytes):
        resp = MagicMock()
        resp.read = MagicMock(return_value=content)
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_strips_html_tags(self):
        html = b"<html><body><p>Hello <b>world</b></p></body></html>"
        with patch("urllib.request.urlopen", return_value=self._mock_urlopen(html)):
            out = tools.fetch_page("https://example.com")
        assert "<b>" not in out
        assert "Hello" in out
        assert "world" in out

    def test_strips_script_and_style(self):
        html = b"<script>alert(1)</script><style>.x{color:red}</style><p>Content</p>"
        with patch("urllib.request.urlopen", return_value=self._mock_urlopen(html)):
            out = tools.fetch_page("https://example.com")
        assert "alert" not in out
        assert "color:red" not in out
        assert "Content" in out

    def test_respects_max_chars(self):
        html = b"<p>" + b"A" * 10000 + b"</p>"
        with patch("urllib.request.urlopen", return_value=self._mock_urlopen(html)):
            out = tools.fetch_page("https://example.com", max_chars=100)
        assert len(out) <= 100

    def test_returns_error_string_on_http_failure(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
            out = tools.fetch_page("https://example.com")
        assert "Could not fetch" in out

    def test_decodes_html_entities(self):
        html = b"<p>AT&amp;T &lt;telco&gt;</p>"
        with patch("urllib.request.urlopen", return_value=self._mock_urlopen(html)):
            out = tools.fetch_page("https://example.com")
        assert "AT&T" in out
        assert "&amp;" not in out


# ── web_search_with_fetch ─────────────────────────────────────────────────────

class TestWebSearchWithFetch:
    def test_includes_snippets_and_calls_fetch(self):
        results = _ddg_results(3)
        with patch("tools.DDGS", return_value=_mock_ddgs(results)), \
             patch("tools.fetch_page", return_value="Page content here") as mock_fetch, \
             patch("tools._summarise_for_voice", return_value="Summary."):
            out = tools.web_search_with_fetch("python")
        mock_fetch.assert_called_once_with("https://example.com/1")
        assert out == "Summary."

    def test_no_results_returns_not_found(self):
        with patch("tools.DDGS", return_value=_mock_ddgs([])):
            out = tools.web_search_with_fetch("noresult12345")
        assert "couldn't find" in out.lower() or "nothing" in out.lower()

    def test_falls_back_to_snippets_when_summarise_returns_empty(self):
        results = _ddg_results(2)
        with patch("tools.DDGS", return_value=_mock_ddgs(results)), \
             patch("tools.fetch_page", return_value="page text"), \
             patch("tools._summarise_for_voice", return_value=""):
            out = tools.web_search_with_fetch("query")
        # fallback to snippets
        assert "Result 1" in out
