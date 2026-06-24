import unittest
from types import SimpleNamespace
from unittest.mock import patch

import context_budget
import model_router
import usage_tracker
from brains import brain_ollama


class ContextBudgetCompilerTests(unittest.TestCase):
    def test_target_tokens_uses_long_context_local_glm_without_cloud_expansion(self):
        self.assertEqual(
            context_budget.target_tokens_for("chat", model="glm-4.7-flash", local=True),
            96_000,
        )
        self.assertEqual(
            context_budget.target_tokens_for("chat", model="gpt-4o", local=False),
            12_000,
        )

    def test_compile_context_blocks_drops_low_priority_when_budget_is_tight(self):
        result = context_budget.compile_context_blocks(
            [
                {"label": "low", "content": "x" * 4000, "priority": 1},
                {"label": "high", "content": "important fact", "priority": 100},
            ],
            target_tokens=1030,
            reserve_response_tokens=1000,
        )

        self.assertIn("important fact", result["text"])
        self.assertNotIn("x" * 100, result["text"])
        self.assertEqual(result["selected"][0]["label"], "high")
        self.assertEqual(result["dropped"][0]["label"], "low")


class ModelRouterContextGovernorTests(unittest.TestCase):
    def test_dynamic_context_is_compiled_before_local_model_call(self):
        captured = {}

        def fake_compile(blocks, **kwargs):
            captured["blocks"] = blocks
            captured["kwargs"] = kwargs
            return {
                "text": "SELECTED_CONTEXT",
                "target_tokens": 12000,
                "base_tokens": 1,
                "reserve_response_tokens": 1024,
                "context_budget_tokens": 100,
                "context_used_tokens": 4,
                "selected": [],
                "dropped": [],
            }

        with patch.object(model_router._smem, "retrieve", return_value=[]), \
             patch.object(model_router._smem, "format_for_prompt", return_value="SEMANTIC_CONTEXT"), \
             patch.object(model_router._m0, "search", return_value=[]), \
             patch.object(model_router._m0, "format_for_prompt", return_value="MEM0_CONTEXT"), \
             patch.object(model_router._repeat_context, "context_for_prompt", return_value="REPEAT_CONTEXT"), \
             patch.object(model_router.vault, "build_context", return_value="VAULT_CONTEXT"), \
             patch.object(model_router._gctx, "context_for_query", return_value="GRAPH_CONTEXT"), \
             patch.object(model_router._context_budget, "compile_context_blocks", side_effect=fake_compile), \
             patch.object(model_router, "_current_mode", "open-source"), \
             patch.object(model_router, "_is_mobile_web_active", return_value=False), \
             patch.object(model_router, "forced_model_status", return_value={"active": False}), \
             patch.object(model_router, "_has_local", return_value=True), \
             patch.object(model_router, "_best_local", return_value="jarvis-local"), \
             patch.object(model_router, "ask_local_stream", return_value=iter(["ok"])) as ask_local:
            stream, _label = model_router.smart_stream("repo context question", tool="chat", prefer_local=True)
            self.assertEqual("".join(stream), "ok")

        self.assertEqual(
            [b["label"] for b in captured["blocks"]],
            ["repeat_context", "vault", "graph", "semantic_hint", "semantic_memory", "mem0"],
        )
        self.assertIn("SELECTED_CONTEXT", ask_local.call_args.kwargs["system_extra"])
        self.assertEqual(
            ask_local.call_args.kwargs["context_budget_report"]["text"],
            "SELECTED_CONTEXT",
        )

    def test_dynamic_context_uses_selected_local_model_budget(self):
        captured = {}

        def fake_compile(blocks, **kwargs):
            captured["kwargs"] = kwargs
            return {
                "text": "",
                "target_tokens": kwargs["target_tokens"],
                "base_tokens": 1,
                "reserve_response_tokens": 1024,
                "context_budget_tokens": 100,
                "context_used_tokens": 0,
                "selected": [],
                "dropped": [],
            }

        with patch.object(model_router._smem, "retrieve", return_value=[]), \
             patch.object(model_router._smem, "format_for_prompt", return_value=""), \
             patch.object(model_router._m0, "search", return_value=[]), \
             patch.object(model_router._m0, "format_for_prompt", return_value=""), \
             patch.object(model_router._repeat_context, "context_for_prompt", return_value=""), \
             patch.object(model_router.vault, "build_context", return_value=""), \
             patch.object(model_router._gctx, "context_for_query", return_value=""), \
             patch.object(model_router._context_budget, "compile_context_blocks", side_effect=fake_compile), \
             patch.object(model_router, "_current_mode", "open-source"), \
             patch.object(model_router, "_is_mobile_web_active", return_value=False), \
             patch.object(model_router, "forced_model_status", return_value={"active": False}), \
             patch.object(model_router, "_has_local", return_value=True), \
             patch.object(model_router, "_best_local", return_value="glm-4.7-flash"), \
             patch.object(model_router, "ask_local_stream", return_value=iter(["ok"])):
            stream, _label = model_router.smart_stream("repo context question", tool="chat", prefer_local=True)
            self.assertEqual("".join(stream), "ok")

        self.assertEqual(captured["kwargs"]["target_tokens"], 96_000)


class OllamaPromptFitTests(unittest.TestCase):
    def test_local_stream_fits_against_full_prompt_and_sets_glm_context(self):
        captured = {}

        class FakeClient:
            def chat(self, **kwargs):
                captured["chat_kwargs"] = kwargs
                return iter([
                    SimpleNamespace(
                        message=SimpleNamespace(content="This local response is long enough to flush."),
                        prompt_eval_count=123,
                        eval_count=9,
                    )
                ])

        def fake_fit(prompt, model):
            captured["fit_prompt"] = prompt
            return model

        with patch.object(brain_ollama, "_client", return_value=FakeClient()), \
             patch.object(brain_ollama, "get_best_available", side_effect=lambda model: model), \
             patch.object(brain_ollama, "_fits_local", side_effect=fake_fit), \
             patch.object(brain_ollama.mem, "get_context", return_value=""), \
             patch.object(brain_ollama, "SYSTEM_PROMPT", "BASE_SYSTEM"):
            chunks = list(brain_ollama.ask_local_stream(
                "please answer this local context test",
                model="glm-4.7-flash",
                system_extra="FULL_CONTEXT_MARKER",
            ))

        self.assertTrue(chunks)
        self.assertIn("BASE_SYSTEM", captured["fit_prompt"])
        self.assertIn("FULL_CONTEXT_MARKER", captured["fit_prompt"])
        self.assertEqual(captured["chat_kwargs"]["options"]["num_ctx"], 131072)

    def test_track_context_drops_oldest_messages_before_ollama_call(self):
        captured = {}

        class FakeClient:
            def chat(self, **kwargs):
                captured["chat_kwargs"] = kwargs
                return iter([
                    SimpleNamespace(
                        message=SimpleNamespace(content="Final local answer is ready."),
                        prompt_eval_count=None,
                        eval_count=None,
                    )
                ])

        old_message = "OLD_CONTEXT_MARKER " + ("x" * 2500)
        prompt_messages = [
            {"role": "user", "content": old_message},
            {"role": "assistant", "content": "old assistant response " + ("y" * 800)},
            {"role": "user", "content": "CURRENT_REQUEST_MARKER"},
        ]
        recorded = {}

        def fake_record(**kwargs):
            recorded["kwargs"] = kwargs
            return {}

        with patch.object(brain_ollama, "_client", return_value=FakeClient()), \
             patch.object(brain_ollama, "get_best_available", side_effect=lambda model: model), \
             patch.object(brain_ollama, "_fits_local", side_effect=lambda prompt, model: model), \
             patch.object(brain_ollama.context_budget, "target_tokens_for", return_value=1100) as target_for, \
             patch.object(brain_ollama.mem, "get_context", return_value=""), \
             patch.object(brain_ollama.ctx, "begin_turn", return_value=None), \
             patch.object(brain_ollama.ctx, "end_turn", return_value=None), \
             patch.object(brain_ollama.ctx, "build_prompt_state", return_value=("SYSTEM", prompt_messages, {})), \
             patch.object(brain_ollama.usage_tracker, "record", side_effect=fake_record):
            chunks = list(brain_ollama.ask_local_stream(
                "CURRENT_REQUEST_MARKER",
                model="glm-4.7-flash",
                track_context=True,
            ))

        self.assertTrue(chunks)
        sent_text = brain_ollama._messages_text(captured["chat_kwargs"]["messages"])
        self.assertNotIn("OLD_CONTEXT_MARKER", sent_text)
        self.assertIn("CURRENT_REQUEST_MARKER", sent_text)
        target_for.assert_called_once_with("chat", model="glm-4.7-flash", local=True)
        conversation_budget = recorded["kwargs"]["metadata"]["conversation_budget"]
        self.assertGreaterEqual(conversation_budget["dropped_message_count"], 1)
        self.assertGreater(conversation_budget["dropped_message_tokens"], 0)


class UsageTrackerContextBudgetTests(unittest.TestCase):
    def test_summarize_aggregates_context_budget_metadata(self):
        rows = [
            {
                "seq": 1,
                "timestamp": "2026-06-13T00:00:00+00:00",
                "provider": "ollama",
                "model": "glm-4.7-flash",
                "local": True,
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
                "metadata": {
                    "context_budget": {
                        "target_tokens": 12000,
                        "context_budget_tokens": 9000,
                        "context_used_tokens": 400,
                        "selected": [{"label": "vault"}],
                        "dropped": [{"label": "mem0"}],
                    }
                },
            },
            {
                "seq": 2,
                "timestamp": "2026-06-13T00:01:00+00:00",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "local": False,
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "total_tokens": 11,
                "metadata": {
                    "conversation_budget": {
                        "original_prompt_tokens": 2000,
                        "final_prompt_tokens": 900,
                        "dropped_message_count": 2,
                        "dropped_message_tokens": 1100,
                        "over_budget": False,
                    }
                },
            },
        ]
        with patch.object(usage_tracker, "entries", return_value=rows):
            summary = usage_tracker.summarize(hours=24, include_recent=0)

        context = summary["context_budget"]
        self.assertEqual(context["call_count"], 1)
        self.assertEqual(context["selected_block_count"], 1)
        self.assertEqual(context["dropped_block_count"], 1)
        self.assertEqual(context["context_used_tokens"], 400)
        self.assertEqual(context["average_used_tokens"], 400)
        conversation = summary["conversation_budget"]
        self.assertEqual(conversation["call_count"], 1)
        self.assertEqual(conversation["dropped_message_count"], 2)
        self.assertEqual(conversation["dropped_message_tokens"], 1100)
        self.assertEqual(conversation["average_final_prompt_tokens"], 900)


if __name__ == "__main__":
    unittest.main()
