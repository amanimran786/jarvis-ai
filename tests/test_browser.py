"""
Unit tests for local_runtime/local_browser.py — CDP browser harness.

Tests cover:
  - is_available returns False when Chrome debug port not reachable
  - is_available returns True when debug port responds
  - BrowserSession._load_skills reads persisted selectors
  - BrowserSession._save_skill writes and persists selectors
  - BrowserSession.navigate sends Page.navigate command
  - BrowserSession.get_text executes document.body.innerText
  - BrowserSession.get_article_text tries skill first, falls back
  - fetch_page one-shot usage
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock, mock_open

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from local_runtime import local_browser


class IsAvailableTests(unittest.TestCase):
    def test_is_available_returns_false_when_connection_refused(self):
        """is_available must return False when Chrome debug port not reachable."""
        import urllib.error

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            result = local_browser.is_available()
        self.assertFalse(result)

    def test_is_available_returns_true_when_port_responds(self):
        """is_available must return True on 200 response from /json/list."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=None)
        with patch("urllib.request.urlopen", return_value=mock_response):
            result = local_browser.is_available()
        self.assertTrue(result)

    def test_is_available_returns_false_on_http_error(self):
        """is_available must handle HTTP errors gracefully."""
        import urllib.error

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("url", 404, "Not found", {}, None),
        ):
            result = local_browser.is_available()
        self.assertFalse(result)

    def test_is_available_returns_false_on_timeout(self):
        """is_available must handle connection timeouts."""
        with patch("urllib.request.urlopen", side_effect=TimeoutError("Timeout")):
            result = local_browser.is_available()
        self.assertFalse(result)


class BrowserSessionLoadSkillsTests(unittest.TestCase):
    def test_load_skills_returns_empty_dict_when_file_missing(self):
        """_load_skills must return {} when skills file doesn't exist."""
        with patch("os.path.exists", return_value=False):
            session = local_browser.BrowserSession()
            self.assertEqual(session._skills, {})

    def test_load_skills_parses_json_file(self):
        """_load_skills must read and parse ~/.jarvis_browser_skills.json."""
        skills_data = {
            "example.com": {
                "selector": "article.content",
                "description": "article content div",
            }
        }
        mock_file = mock_open(read_data=json.dumps(skills_data))
        with patch("os.path.exists", return_value=True), patch(
            "builtins.open", mock_file
        ):
            session = local_browser.BrowserSession()
        self.assertEqual(session._skills["example.com"]["selector"], "article.content")

    def test_load_skills_returns_empty_on_json_error(self):
        """_load_skills must return {} if file is malformed."""
        mock_file = mock_open(read_data="not valid json")
        with patch("os.path.exists", return_value=True), patch(
            "builtins.open", mock_file
        ):
            session = local_browser.BrowserSession()
        self.assertEqual(session._skills, {})


class BrowserSessionSaveSkillTests(unittest.TestCase):
    def test_save_skill_writes_json_file(self):
        """_save_skill must persist skill to ~/.jarvis_browser_skills.json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_file = os.path.join(tmpdir, "skills.json")
            with patch.object(
                local_browser, "SKILLS_FILE", skills_file
            ), patch.object(local_browser.BrowserSession, "_load_skills", return_value={}):
                session = local_browser.BrowserSession()
                session._save_skill("example.com", "article.body", "main article")

            # Verify file was written
            self.assertTrue(os.path.exists(skills_file))
            with open(skills_file) as f:
                data = json.load(f)
            self.assertEqual(data["example.com"]["selector"], "article.body")
            self.assertEqual(data["example.com"]["description"], "main article")

    def test_save_skill_includes_timestamp(self):
        """_save_skill must include timestamp in persisted skill."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_file = os.path.join(tmpdir, "skills.json")
            with patch.object(
                local_browser, "SKILLS_FILE", skills_file
            ), patch.object(local_browser.BrowserSession, "_load_skills", return_value={}):
                session = local_browser.BrowserSession()
                session._save_skill("example.com", "article.content", "test")

            with open(skills_file) as f:
                data = json.load(f)
            self.assertIn("timestamp", data["example.com"])
            self.assertIsInstance(data["example.com"]["timestamp"], (int, float))

    def test_save_skill_handles_write_error_gracefully(self):
        """_save_skill must not raise on file write error."""
        with patch("os.makedirs", side_effect=OSError("Permission denied")):
            with patch.object(
                local_browser.BrowserSession, "_load_skills", return_value={}
            ):
                session = local_browser.BrowserSession()
                # Should not raise
                session._save_skill("example.com", "selector", "desc")


class BrowserSessionNavigateTests(unittest.TestCase):
    def test_navigate_sends_page_navigate_command(self):
        """navigate must send Page.navigate command via CDP."""
        with patch.object(
            local_browser.BrowserSession, "_get_or_open_tab", return_value=True
        ), patch.object(
            local_browser.BrowserSession, "_send_command"
        ) as mock_send:
            mock_send.return_value = {"result": {"frameId": "123"}}
            session = local_browser.BrowserSession()
            result = session.navigate("https://example.com", wait_ms=100)

        self.assertTrue(result)
        mock_send.assert_called_once()
        call_args = mock_send.call_args
        self.assertEqual(call_args[0][0], "Page.navigate")
        self.assertEqual(call_args[0][1]["url"], "https://example.com")

    def test_navigate_returns_false_when_tab_unavailable(self):
        """navigate must return False if _get_or_open_tab fails."""
        with patch.object(
            local_browser.BrowserSession, "_get_or_open_tab", return_value=False
        ):
            session = local_browser.BrowserSession()
            result = session.navigate("https://example.com")
        self.assertFalse(result)

    def test_navigate_returns_false_on_command_failure(self):
        """navigate must return False if Page.navigate returns no frameId."""
        with patch.object(
            local_browser.BrowserSession, "_get_or_open_tab", return_value=True
        ), patch.object(
            local_browser.BrowserSession, "_send_command", return_value=None
        ):
            session = local_browser.BrowserSession()
            result = session.navigate("https://example.com")
        self.assertFalse(result)


class BrowserSessionGetTextTests(unittest.TestCase):
    def test_get_text_executes_innertext_expression(self):
        """get_text must execute document.body.innerText via Runtime.evaluate."""
        with patch.object(
            local_browser.BrowserSession, "_get_or_open_tab", return_value=True
        ), patch.object(
            local_browser.BrowserSession, "_send_command"
        ) as mock_send:
            mock_send.return_value = {
                "result": {"result": {"value": "Hello, World!"}}
            }
            session = local_browser.BrowserSession()
            text = session.get_text()

        self.assertEqual(text, "Hello, World!")
        call_args = mock_send.call_args
        self.assertEqual(call_args[0][0], "Runtime.evaluate")
        self.assertIn(
            "document.body.innerText", call_args[0][1]["expression"]
        )

    def test_get_text_returns_empty_on_failure(self):
        """get_text must return empty string if command fails."""
        with patch.object(
            local_browser.BrowserSession, "_get_or_open_tab", return_value=False
        ):
            session = local_browser.BrowserSession()
            text = session.get_text()
        self.assertEqual(text, "")


class BrowserSessionGetArticleTextTests(unittest.TestCase):
    def test_get_article_text_tries_skill_first(self):
        """get_article_text must try known skill before fallback."""
        with patch.object(
            local_browser.BrowserSession, "_load_skills", return_value={}
        ), patch.object(
            local_browser.BrowserSession, "navigate", return_value=True
        ), patch.object(
            local_browser.BrowserSession, "find_text"
        ) as mock_find:
            mock_find.return_value = ["Skilled result"]
            session = local_browser.BrowserSession()
            session._skills["example.com"] = {"selector": "article.main"}
            text = session.get_article_text("https://example.com/article")

        self.assertEqual(text, "Skilled result")
        mock_find.assert_called_once_with("article.main")

    def test_get_article_text_falls_back_to_get_text(self):
        """get_article_text must fallback to get_text if skill returns nothing."""
        with patch.object(
            local_browser.BrowserSession, "_load_skills", return_value={}
        ), patch.object(
            local_browser.BrowserSession, "navigate", return_value=True
        ), patch.object(
            local_browser.BrowserSession, "find_text", return_value=[]
        ), patch.object(
            local_browser.BrowserSession, "get_text", return_value="Fallback text"
        ), patch.object(
            local_browser.BrowserSession, "_save_skill"
        ):
            session = local_browser.BrowserSession()
            session._skills["example.com"] = {"selector": "article.main"}
            text = session.get_article_text("https://example.com/article")

        self.assertEqual(text, "Fallback text")

    def test_get_article_text_saves_working_approach(self):
        """get_article_text must save fallback approach when skill fails."""
        with patch.object(
            local_browser.BrowserSession, "_load_skills", return_value={}
        ), patch.object(
            local_browser.BrowserSession, "navigate", return_value=True
        ), patch.object(
            local_browser.BrowserSession, "find_text", return_value=[]
        ), patch.object(
            local_browser.BrowserSession, "get_text", return_value="Text found"
        ), patch.object(
            local_browser.BrowserSession, "_save_skill"
        ) as mock_save:
            session = local_browser.BrowserSession()
            session._skills["example.com"] = {"selector": "article.main"}
            session.get_article_text("https://example.com/article")

        # Should save fallback skill for this domain
        mock_save.assert_called()
        call_args = mock_save.call_args
        self.assertEqual(call_args[0][0], "example.com")
        self.assertEqual(call_args[0][1], "body")

    def test_get_article_text_returns_empty_on_navigate_failure(self):
        """get_article_text must return empty string if navigate fails."""
        with patch.object(
            local_browser.BrowserSession, "navigate", return_value=False
        ):
            session = local_browser.BrowserSession()
            text = session.get_article_text("https://example.com/article")
        self.assertEqual(text, "")


class FetchPageTests(unittest.TestCase):
    def test_fetch_page_returns_empty_when_unavailable(self):
        """fetch_page must return empty string if Chrome not available."""
        with patch.object(local_browser, "is_available", return_value=False):
            result = local_browser.fetch_page("https://example.com")
        self.assertEqual(result, "")

    def test_fetch_page_opens_session_navigates_closes(self):
        """fetch_page must open session, navigate, get_text, and close."""
        with patch.object(local_browser, "is_available", return_value=True), patch(
            "local_runtime.local_browser.BrowserSession"
        ) as mock_session_class:
            mock_session = MagicMock()
            mock_session_class.return_value = mock_session
            mock_session.navigate.return_value = True
            mock_session.get_text.return_value = "Page content"

            result = local_browser.fetch_page("https://example.com", wait_ms=1000)

        self.assertEqual(result, "Page content")
        mock_session.navigate.assert_called_once_with("https://example.com", 1000)
        mock_session.get_text.assert_called_once()
        mock_session.close.assert_called_once()

    def test_fetch_page_returns_empty_on_navigate_failure(self):
        """fetch_page must return empty string if navigate fails."""
        with patch.object(local_browser, "is_available", return_value=True), patch(
            "local_runtime.local_browser.BrowserSession"
        ) as mock_session_class:
            mock_session = MagicMock()
            mock_session_class.return_value = mock_session
            mock_session.navigate.return_value = False

            result = local_browser.fetch_page("https://example.com")

        self.assertEqual(result, "")
        mock_session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
