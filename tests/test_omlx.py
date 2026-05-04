"""
Unit tests for local_runtime/local_omlx.py — oMLX inference backend client.

Tests cover:
  - is_running returns False when connection is refused
  - is_running returns True when server responds
  - status includes required keys
  - list_models returns empty list when not running
  - chat returns None when not running
  - is_better_for_long_context false when not running
  - is_better_for_long_context true when running and large context
  - is_better_for_long_context false for short context
"""

import json
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from local_runtime import local_omlx


class IsRunningTests(unittest.TestCase):
    def test_is_running_returns_false_when_connection_refused(self):
        """is_running must gracefully handle connection refusal."""
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
            result = local_omlx.is_running()
        self.assertFalse(result)

    def test_is_running_returns_true_when_server_responds(self):
        """is_running must return True on 200 response."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=None)
        with patch("urllib.request.urlopen", return_value=mock_response):
            result = local_omlx.is_running()
        self.assertTrue(result)

    def test_is_running_returns_false_on_http_error(self):
        """is_running must handle HTTP errors."""
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError("url", 500, "Server error", {}, None)):
            result = local_omlx.is_running()
        self.assertFalse(result)

    def test_is_running_returns_false_on_timeout(self):
        """is_running must handle timeouts."""
        with patch("urllib.request.urlopen", side_effect=TimeoutError("Timeout")):
            result = local_omlx.is_running()
        self.assertFalse(result)


class StatusTests(unittest.TestCase):
    def test_status_includes_required_keys(self):
        """status() must return all required fields."""
        with patch("local_runtime.local_omlx.is_running", return_value=False):
            result = local_omlx.status()
        self.assertIn("running", result)
        self.assertIn("url", result)
        self.assertIn("models", result)
        self.assertIn("error", result)

    def test_status_when_not_running(self):
        """status() must report not running with error message."""
        with patch("local_runtime.local_omlx.is_running", return_value=False):
            result = local_omlx.status()
        self.assertFalse(result["running"])
        self.assertEqual(result["models"], [])
        self.assertIsNotNone(result["error"])

    def test_status_when_running(self):
        """status() must report running with models list."""
        with patch("local_runtime.local_omlx.is_running", return_value=True), \
             patch("local_runtime.local_omlx.list_models", return_value=["qwen3:8b", "llama2:7b"]):
            result = local_omlx.status()
        self.assertTrue(result["running"])
        self.assertEqual(result["models"], ["qwen3:8b", "llama2:7b"])
        self.assertIsNone(result["error"])


class ListModelsTests(unittest.TestCase):
    def test_list_models_returns_empty_when_not_running(self):
        """list_models must return [] when oMLX is not running."""
        with patch("local_runtime.local_omlx.is_running", return_value=False):
            result = local_omlx.list_models()
        self.assertEqual(result, [])

    def test_list_models_parses_omlx_response(self):
        """list_models must parse OpenAI-format model list."""
        mock_response = MagicMock()
        omlx_response = {
            "data": [
                {"id": "qwen3:8b", "object": "model"},
                {"id": "llama2:7b", "object": "model"},
            ]
        }
        mock_response.read.return_value = json.dumps(omlx_response).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=None)
        with patch("local_runtime.local_omlx.is_running", return_value=True), \
             patch("urllib.request.urlopen", return_value=mock_response):
            result = local_omlx.list_models()
        self.assertEqual(result, ["qwen3:8b", "llama2:7b"])

    def test_list_models_handles_error(self):
        """list_models must return [] on request error."""
        with patch("local_runtime.local_omlx.is_running", return_value=True), \
             patch("urllib.request.urlopen", side_effect=Exception("Network error")):
            result = local_omlx.list_models()
        self.assertEqual(result, [])


class ChatTests(unittest.TestCase):
    def test_chat_returns_none_when_not_running(self):
        """chat must return None when oMLX is not running."""
        with patch("local_runtime.local_omlx.is_running", return_value=False):
            result = local_omlx.chat("qwen3:8b", [{"role": "user", "content": "Hi"}])
        self.assertIsNone(result)

    def test_chat_raises_on_streaming(self):
        """chat must raise NotImplementedError for streaming."""
        with patch("local_runtime.local_omlx.is_running", return_value=True):
            with self.assertRaises(NotImplementedError):
                local_omlx.chat("qwen3:8b", [{"role": "user", "content": "Hi"}], stream=True)

    def test_chat_sends_openai_format_request(self):
        """chat must send OpenAI-format request to oMLX."""
        mock_response = MagicMock()
        openai_response = {
            "id": "chatcmpl-123",
            "choices": [{"message": {"content": "Hello!"}}]
        }
        mock_response.read.return_value = json.dumps(openai_response).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=None)
        with patch("local_runtime.local_omlx.is_running", return_value=True), \
             patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
            result = local_omlx.chat("qwen3:8b", [{"role": "user", "content": "Hi"}])
        self.assertIsNotNone(result)
        self.assertEqual(result["choices"][0]["message"]["content"], "Hello!")
        # Verify the request was made to the correct endpoint
        mock_urlopen.assert_called_once()
        call_args = mock_urlopen.call_args
        self.assertIn("/v1/chat/completions", call_args[0][0].get_full_url())

    def test_chat_returns_none_on_error(self):
        """chat must return None on request error."""
        with patch("local_runtime.local_omlx.is_running", return_value=True), \
             patch("urllib.request.urlopen", side_effect=Exception("Network error")):
            result = local_omlx.chat("qwen3:8b", [{"role": "user", "content": "Hi"}])
        self.assertIsNone(result)


class IsBetterForLongContextTests(unittest.TestCase):
    def test_is_better_for_long_context_false_when_not_running(self):
        """is_better_for_long_context must be False when oMLX is not running."""
        with patch("local_runtime.local_omlx.is_running", return_value=False):
            result = local_omlx.is_better_for_long_context(10000)
        self.assertFalse(result)

    def test_is_better_for_long_context_true_when_running_and_large_context(self):
        """is_better_for_long_context must be True when running and >8000 tokens."""
        with patch("local_runtime.local_omlx.is_running", return_value=True):
            result = local_omlx.is_better_for_long_context(10000)
        self.assertTrue(result)

    def test_is_better_for_long_context_false_for_short_context(self):
        """is_better_for_long_context must be False for <8000 tokens even when running."""
        with patch("local_runtime.local_omlx.is_running", return_value=True):
            result = local_omlx.is_better_for_long_context(5000)
        self.assertFalse(result)

    def test_is_better_for_long_context_threshold_boundary(self):
        """is_better_for_long_context must use > 8000, not >= 8000."""
        with patch("local_runtime.local_omlx.is_running", return_value=True):
            # Exactly 8000 should be False
            self.assertFalse(local_omlx.is_better_for_long_context(8000))
            # 8001 should be True
            self.assertTrue(local_omlx.is_better_for_long_context(8001))


if __name__ == "__main__":
    unittest.main()
