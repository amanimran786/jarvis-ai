"""Every local Ollama call must send an explicit num_ctx.

Without it, models not covered by _ollama_options_for_model run at the
Ollama server default (~4K) and silently truncate prompts that the
context-budget capper sized for 24-96K.
"""

from types import SimpleNamespace
from unittest.mock import patch

from brains import brain_ollama


class _FakeClient:
    def __init__(self, content="answer."):
        self.calls = []
        self._content = content

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        response = SimpleNamespace(
            message=SimpleNamespace(content=self._content, tool_calls=[]),
            prompt_eval_count=10,
            eval_count=4,
        )
        if kwargs.get("stream"):
            return iter([response])
        return response


def test_ask_local_structured_sends_num_ctx():
    client = _FakeClient('{"intent": "chat"}')
    with patch.object(brain_ollama, "_structured_client", return_value=client), \
         patch.object(brain_ollama, "_fits_local", side_effect=lambda prompt, model: model), \
         patch.object(brain_ollama, "get_best_available", side_effect=lambda model: model), \
         patch.object(brain_ollama.context_budget, "target_tokens_for", return_value=24_000), \
         patch.object(brain_ollama.usage_tracker, "record"):
        brain_ollama.ask_local_structured("classify this", schema="json", model="llama3.1:8b")

    options = client.calls[0]["options"]
    assert options["num_ctx"] == 24_000
    assert options["temperature"] == 0


def test_ask_local_stream_sends_num_ctx_for_unmatched_model():
    client = _FakeClient("hello.")
    with patch.object(brain_ollama, "_client", return_value=client), \
         patch.object(brain_ollama, "_fits_local", side_effect=lambda prompt, model: model), \
         patch.object(brain_ollama, "get_best_available", side_effect=lambda model: model), \
         patch.object(brain_ollama.mem, "get_context", return_value=""), \
         patch.object(brain_ollama.context_budget, "target_tokens_for", return_value=24_000), \
         patch.object(brain_ollama.usage_tracker, "record"):
        list(brain_ollama.ask_local_stream("hi", model="llama3.1:8b"))

    options = client.calls[0]["options"]
    assert options["num_ctx"] == 24_000


def test_ask_local_stream_model_specific_num_ctx_wins():
    client = _FakeClient("hello.")
    with patch.object(brain_ollama, "_client", return_value=client), \
         patch.object(brain_ollama, "_fits_local", side_effect=lambda prompt, model: model), \
         patch.object(brain_ollama, "get_best_available", side_effect=lambda model: model), \
         patch.object(brain_ollama.mem, "get_context", return_value=""), \
         patch.object(brain_ollama.usage_tracker, "record"), \
         patch.dict("os.environ", {"GLM_CTX": "131072"}):
        list(brain_ollama.ask_local_stream("hi", model="glm-4.7-flash"))

    options = client.calls[0]["options"]
    # _ollama_options_for_model's explicit value must not be overridden
    # by the context-budget default.
    assert options["num_ctx"] == 131_072
