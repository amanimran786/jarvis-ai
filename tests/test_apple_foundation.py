import json
import unittest
from unittest.mock import MagicMock, patch

from brains import brain_apple_foundation as apple


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class AppleFoundationTests(unittest.TestCase):
    def tearDown(self):
        apple._availability_checked_at = 0.0
        apple._availability_result = False

    def test_is_available_requires_apple_foundation_model(self):
        payload = {"data": [{"id": "apple-foundationmodel"}]}
        with patch("brains.brain_apple_foundation.APPLE_FOUNDATION_ENABLED", True), \
             patch("brains.brain_apple_foundation.urlopen", return_value=_Response(payload)):
            self.assertTrue(apple.is_available(force_refresh=True))

    def test_is_available_false_when_model_missing(self):
        payload = {"data": [{"id": "gemma4:e4b"}]}
        with patch("brains.brain_apple_foundation.APPLE_FOUNDATION_ENABLED", True), \
             patch("brains.brain_apple_foundation.urlopen", return_value=_Response(payload)):
            self.assertFalse(apple.is_available(force_refresh=True))

    def test_is_available_false_when_disabled_by_default(self):
        payload = {"data": [{"id": "apple-foundationmodel"}]}
        with patch("brains.brain_apple_foundation.APPLE_FOUNDATION_ENABLED", False), \
             patch("brains.brain_apple_foundation.urlopen", return_value=_Response(payload)) as urlopen_mock:
            self.assertFalse(apple.is_available(force_refresh=True))

        urlopen_mock.assert_not_called()

    def test_is_available_rejects_non_loopback_base_url(self):
        payload = {"data": [{"id": "apple-foundationmodel"}]}
        with patch("brains.brain_apple_foundation.APPLE_FOUNDATION_ENABLED", True), \
             patch("brains.brain_apple_foundation.APPLE_FOUNDATION_BASE_URL", "https://example.com/v1"), \
             patch("brains.brain_apple_foundation.urlopen", return_value=_Response(payload)) as urlopen_mock:
            self.assertFalse(apple.is_available(force_refresh=True))

        urlopen_mock.assert_not_called()

    def test_stream_raises_when_unavailable_on_strict_path(self):
        with patch("brains.brain_apple_foundation.is_available", return_value=False):
            stream = apple.ask_apple_foundation_stream("hello", raise_on_error=True)
            with self.assertRaises(RuntimeError):
                list(stream)

    def test_stream_records_local_usage(self):
        chunk = MagicMock()
        chunk.usage = None
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta.content = "hello"

        client = MagicMock()
        client.chat.completions.create.return_value = [chunk]

        with patch("brains.brain_apple_foundation.is_available", return_value=True), \
             patch("brains.brain_apple_foundation._client", return_value=client), \
             patch("brains.brain_apple_foundation.usage_tracker.record") as record_mock:
            text = "".join(apple.ask_apple_foundation_stream("Say hello."))

        self.assertEqual(text, "hello")
        record_mock.assert_called_once()
        self.assertEqual(record_mock.call_args.kwargs["provider"], "apple_foundation")
        self.assertTrue(record_mock.call_args.kwargs["local"])


if __name__ == "__main__":
    unittest.main()
