import unittest
from unittest.mock import patch

import repeat_context


class RepeatContextTests(unittest.TestCase):
    def setUp(self):
        repeat_context._cache.clear()

    def tearDown(self):
        repeat_context._cache.clear()

    def test_context_for_prompt_uses_vault_lessons_without_llm_call(self):
        vault_hit = {
            "score": 14,
            "path": "sessions/lessons.md",
            "title": "Session Lessons",
            "excerpt": "Capture only lessons backed by repeated behavior, explicit user correction, eval failure, or verified repo evidence.",
            "matched_heading": "Capture Rules",
            "citation": {"label": "sessions/lessons.md > Capture Rules"},
        }

        with patch("vault.search", return_value=[vault_hit]), \
             patch("memory.get_recent_conversations", return_value=[]), \
             patch("mem0_layer.search", return_value=[]):
            text = repeat_context.context_for_prompt(
                "I do not want to burn tokens recognizing the same mistake again"
            )

        self.assertIn("Seen-before context", text)
        self.assertIn("sessions/lessons.md > Capture Rules", text)
        self.assertIn("repeated behavior", text)

    def test_recent_conversation_summary_can_surface_repeat_context(self):
        with patch("vault.search", return_value=[]), \
             patch("memory.get_recent_conversations", return_value=[
                 {
                     "date": "2026-06-15 14:10",
                     "summary": "Conversation about context window token usage and using Obsidian vault memory to avoid repeated requests.",
                 }
             ]), \
             patch("mem0_layer.search", return_value=[]):
            text = repeat_context.context_for_prompt(
                "How do we use Obsidian vault memory to avoid repeated context window token usage?"
            )

        self.assertIn("memory conversation 2026-06-15 14:10", text)
        self.assertIn("Obsidian vault memory", text)

    def test_empty_query_returns_empty_context(self):
        self.assertEqual(repeat_context.context_for_prompt(""), "")

    def test_result_is_cached_by_request_fingerprint(self):
        hit = {
            "score": 10,
            "excerpt": "Do not repeat context already established.",
            "citation": {"label": "brain/context-budget.md"},
        }
        with patch("vault.search", return_value=[hit]) as search_mock, \
             patch("memory.get_recent_conversations", return_value=[]), \
             patch("mem0_layer.search", return_value=[]):
            first = repeat_context.context_for_prompt("do not repeat the same context")
            second = repeat_context.context_for_prompt("do not repeat the same context")

        self.assertEqual(first, second)
        self.assertEqual(search_mock.call_count, 3)


if __name__ == "__main__":
    unittest.main()
