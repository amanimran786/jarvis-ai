"""Tests for harness/web_search.py — all network and LLM calls mocked."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import urllib.error

from harness import web_search as ws


# ── helpers ───────────────────────────────────────────────────────────────────

def _ddg_results(n: int = 3, body_len: int = 80) -> list[dict]:
    body = "x" * body_len
    return [
        {"title": f"Result {i}", "body": body, "href": f"https://example.com/{i}"}
        for i in range(1, n + 1)
    ]


def _mock_ddgs(results):
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.text = MagicMock(return_value=iter(results))
    return ctx


def _mock_urlopen(content: bytes):
    resp = MagicMock()
    resp.read = MagicMock(return_value=content)
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ── search() ─────────────────────────────────────────────────────────────────

class TestSearch:
    def test_returns_numbered_results_with_urls(self):
        with patch("harness.web_search.DDGS", return_value=_mock_ddgs(_ddg_results(2))), \
             patch("harness.web_search._summarise", return_value=""):
            out = ws.search("python", summarise=False)
        assert "[1]" in out and "[2]" in out
        assert "https://example.com/1" in out

    def test_no_results_returns_not_found(self):
        with patch("harness.web_search.DDGS", return_value=_mock_ddgs([])):
            out = ws.search("xyzzy")
        assert "couldn't find" in out.lower() or "nothing" in out.lower()

    def test_ddgs_exception_returns_error(self):
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(side_effect=RuntimeError("rate limited"))
        ctx.__exit__ = MagicMock(return_value=False)
        with patch("harness.web_search.DDGS", return_value=ctx):
            out = ws.search("query")
        assert "Search failed" in out or "rate limited" in out

    def test_summarise_called_when_raw_exceeds_threshold(self):
        # body_len=80, 5 results → raw > 300 chars
        with patch("harness.web_search.DDGS", return_value=_mock_ddgs(_ddg_results(5))), \
             patch("harness.web_search._summarise", return_value="summary text") as mock_sum:
            out = ws.search("query", summarise=True)
        mock_sum.assert_called_once()
        assert out == "summary text"

    def test_summarise_skipped_when_disabled(self):
        with patch("harness.web_search.DDGS", return_value=_mock_ddgs(_ddg_results(5))), \
             patch("harness.web_search._summarise") as mock_sum:
            ws.search("query", summarise=False)
        mock_sum.assert_not_called()

    def test_falls_back_to_raw_when_summarise_returns_empty(self):
        with patch("harness.web_search.DDGS", return_value=_mock_ddgs(_ddg_results(5))), \
             patch("harness.web_search._summarise", return_value=""):
            out = ws.search("query", summarise=True)
        assert "[1]" in out  # raw snippets returned

    def test_max_results_passed_to_ddgs(self):
        with patch("harness.web_search.DDGS", return_value=_mock_ddgs(_ddg_results(3))) as mock_cls:
            ws.search("query", max_results=7, summarise=False)
        mock_cls.return_value.text.assert_called_once_with("query", max_results=7)


# ── fetch_page() ─────────────────────────────────────────────────────────────

class TestFetchPage:
    def test_strips_html_tags(self):
        html = b"<html><body><p>Hello <b>world</b></p></body></html>"
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(html)):
            out = ws.fetch_page("https://example.com")
        assert "<b>" not in out
        assert "Hello" in out and "world" in out

    def test_strips_scripts_and_styles(self):
        html = b"<script>alert(1)</script><style>.x{}</style><p>Content</p>"
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(html)):
            out = ws.fetch_page("https://example.com")
        assert "alert" not in out
        assert "Content" in out

    def test_respects_max_chars(self):
        html = b"<p>" + b"A" * 10000 + b"</p>"
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(html)):
            out = ws.fetch_page("https://example.com", max_chars=200)
        assert len(out) <= 200

    def test_decodes_html_entities(self):
        html = b"<p>AT&amp;T &lt;co&gt;</p>"
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(html)):
            out = ws.fetch_page("https://example.com")
        assert "AT&T" in out

    def test_url_error_returns_could_not_fetch(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
            out = ws.fetch_page("https://example.com")
        assert "Could not fetch" in out

    def test_unexpected_exception_returns_could_not_fetch(self):
        with patch("urllib.request.urlopen", side_effect=OSError("socket closed")):
            out = ws.fetch_page("https://example.com")
        assert "Could not fetch" in out


# ── search_and_fetch() ───────────────────────────────────────────────────────

class TestSearchAndFetch:
    def test_fetches_top_result_url(self):
        results = _ddg_results(3)
        with patch("harness.web_search.DDGS", return_value=_mock_ddgs(results)), \
             patch("harness.web_search.fetch_page", return_value="page content") as mock_fp, \
             patch("harness.web_search._summarise", return_value="Summary."):
            out = ws.search_and_fetch("python")
        mock_fp.assert_called_once_with("https://example.com/1")
        assert out == "Summary."

    def test_no_results_returns_not_found(self):
        with patch("harness.web_search.DDGS", return_value=_mock_ddgs([])):
            out = ws.search_and_fetch("nothing")
        assert "couldn't find" in out.lower() or "nothing" in out.lower()

    def test_falls_back_to_snippets_when_summarise_empty(self):
        results = _ddg_results(2)
        with patch("harness.web_search.DDGS", return_value=_mock_ddgs(results)), \
             patch("harness.web_search.fetch_page", return_value="page text"), \
             patch("harness.web_search._summarise", return_value=""):
            out = ws.search_and_fetch("query")
        assert "[1]" in out  # snippet fallback

    def test_skips_fetch_when_no_href(self):
        results = [{"title": "R", "body": "body", "href": ""}]
        with patch("harness.web_search.DDGS", return_value=_mock_ddgs(results)), \
             patch("harness.web_search.fetch_page") as mock_fp, \
             patch("harness.web_search._summarise", return_value="summary"):
            ws.search_and_fetch("query")
        mock_fp.assert_not_called()


# ── _summarise() ─────────────────────────────────────────────────────────────

class TestSummarise:
    def test_uses_get_best_available_not_hardcoded_model(self):
        long_summary = "This is a sufficiently long summary that exceeds the twenty character minimum."
        with patch("brains.brain_ollama.get_best_available", return_value="glm-flash") as mock_gba, \
             patch("brains.brain_ollama.ask_local", return_value=long_summary):
            result = ws._summarise("raw search results text", "query")
        mock_gba.assert_called_once()
        assert result == long_summary

    def test_returns_empty_on_ollama_unavailable(self):
        with patch("brains.brain_ollama.get_best_available", side_effect=RuntimeError("offline")):
            result = ws._summarise("raw text", "query")
        assert result == ""

    def test_returns_empty_when_summary_too_short(self):
        with patch("brains.brain_ollama.get_best_available", return_value="model"), \
             patch("brains.brain_ollama.ask_local", return_value="ok"):  # 2 chars
            result = ws._summarise("raw text", "query")
        assert result == ""

    def test_timeout_returns_empty(self):
        import time

        def _slow(*a, **kw):
            time.sleep(60)
            return "never"

        import threading
        orig_join = threading.Thread.join

        def _fast_join(self, timeout=None):
            return  # simulate timeout — result_holder stays empty

        with patch("brains.brain_ollama.get_best_available", return_value="model"), \
             patch("brains.brain_ollama.ask_local", side_effect=_slow), \
             patch.object(threading.Thread, "join", _fast_join):
            result = ws._summarise("raw text", "query")
        assert result == ""
