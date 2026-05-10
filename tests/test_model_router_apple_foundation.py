import unittest
from unittest.mock import patch

import model_router


class ModelRouterAppleFoundationTests(unittest.TestCase):
    def test_apple_foundation_guard_requires_mini_chat_and_short_prompt(self):
        with patch("brains.brain_apple_foundation.is_available", return_value=True):
            self.assertTrue(model_router._apple_foundation_available_for("Hi there.", "chat", "mini"))
            self.assertFalse(model_router._apple_foundation_available_for("Hi there.", "chat", "sonnet"))
            self.assertFalse(model_router._apple_foundation_available_for("Hi there.", "terminal", "mini"))
            self.assertFalse(model_router._apple_foundation_available_for("x" * 2100, "chat", "mini"))

    def test_open_source_uses_apple_foundation_when_it_is_only_local_candidate(self):
        previous = model_router.get_mode()
        try:
            model_router.set_mode("open-source")
            with patch("model_router._has_local", return_value=False), \
                 patch("model_router._apple_foundation_available_for", return_value=True), \
                 patch("brains.brain_apple_foundation.ask_apple_foundation_stream", return_value=iter(["apple answer"])), \
                 patch("model_router.ask_stream", side_effect=AssertionError("should not call cloud")), \
                 patch("model_router.ask_local_stream", side_effect=AssertionError("should not call ollama")):
                stream, label = model_router.smart_stream("Hi.", tool="chat")
                text = "".join(stream)
        finally:
            model_router.set_mode(previous)

        self.assertEqual(label, "Apple Foundation")
        self.assertEqual(text, "apple answer")

    def test_apple_failure_falls_back_to_ollama_when_available(self):
        previous = model_router.get_mode()

        def broken_apple(*_args, **_kwargs):
            def _gen():
                raise RuntimeError("apple unavailable")
                yield ""
            return _gen()

        try:
            model_router.set_mode("open-source")
            with patch("model_router._has_local", return_value=True), \
                 patch("model_router._best_local", return_value="jarvis-local"), \
                 patch("model_router._apple_foundation_available_for", return_value=True), \
                 patch("brains.brain_apple_foundation.ask_apple_foundation_stream", side_effect=broken_apple), \
                 patch("model_router.ask_local_stream", return_value=iter(["ollama answer"])), \
                 patch("model_router.ask_stream", side_effect=AssertionError("should not call cloud")):
                stream, label = model_router.smart_stream("Hi.", tool="chat")
                text = "".join(stream)
        finally:
            model_router.set_mode(previous)

        self.assertEqual(label, "Apple Foundation")
        self.assertEqual(text, "ollama answer")


if __name__ == "__main__":
    unittest.main()
