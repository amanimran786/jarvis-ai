"""ask_local_structured usage-summary and telemetry coverage.

Mocks the Ollama structured client and usage_tracker.record — no live Ollama
or disk usage log involved.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from brains import brain_ollama


def _structured_response(content='{"intent": "chat"}', prompt_tokens=10, completion_tokens=4):
    return SimpleNamespace(
        message=SimpleNamespace(content=content),
        prompt_eval_count=prompt_tokens,
        eval_count=completion_tokens,
    )


class _FakeStructuredClient:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _run_structured(client, *, model="llama3.1:8b", raise_on_error=True):
    records = []
    with patch.object(brain_ollama, "_structured_client", return_value=client), \
         patch.object(brain_ollama, "_fits_local", side_effect=lambda prompt, model: model), \
         patch.object(brain_ollama, "get_best_available", side_effect=lambda model: model), \
         patch.object(brain_ollama.context_budget, "target_tokens_for", return_value=24_000), \
         patch.object(brain_ollama.usage_tracker, "record", side_effect=lambda **kw: records.append(kw)):
        result = brain_ollama.ask_local_structured(
            "classify this", schema="json", model=model, raise_on_error=raise_on_error,
        )
    return result, records


def test_structured_call_records_usage_summary_with_token_counts_and_model():
    client = _FakeStructuredClient(_structured_response(prompt_tokens=42, completion_tokens=8))

    content, records = _run_structured(client, model="qwen3:8b")

    assert content == '{"intent": "chat"}'
    assert len(records) == 1
    entry = records[0]
    assert entry["model"] == "qwen3:8b"
    assert entry["provider"] == "ollama"
    assert entry["prompt_tokens"] == 42
    assert entry["completion_tokens"] == 8
    assert entry["total_tokens"] == 50
    assert entry["source"] == "brain_ollama.ask_local_structured"
    assert entry["metadata"]["structured"] is True


def test_structured_call_records_usage_exactly_once_after_the_call():
    client = _FakeStructuredClient(_structured_response())

    _content, records = _run_structured(client)

    assert len(client.calls) == 1
    assert len(records) == 1
    assert records[0]["response_text"] == '{"intent": "chat"}'


def test_empty_response_content_still_records_usage_and_returns_empty_string():
    client = _FakeStructuredClient(_structured_response(content="", prompt_tokens=5, completion_tokens=0))

    content, records = _run_structured(client)

    assert content == ""
    assert len(records) == 1
    assert records[0]["response_text"] == ""
    assert records[0]["completion_tokens"] == 0


def test_api_error_raises_and_does_not_record_usage_when_raise_on_error_true():
    client = _FakeStructuredClient(ConnectionError("ollama down"))

    with pytest.raises(RuntimeError):
        _run_structured(client, raise_on_error=True)


def test_api_error_returns_empty_and_does_not_record_usage_when_raise_on_error_false():
    client = _FakeStructuredClient(ConnectionError("ollama down"))

    content, records = _run_structured(client, raise_on_error=False)

    assert content == ""
    assert records == []


def test_missing_usage_fields_marks_entry_as_estimated_with_null_totals():
    response = SimpleNamespace(message=SimpleNamespace(content="{}"))  # no *_count attrs
    client = _FakeStructuredClient(response)

    _content, records = _run_structured(client)

    entry = records[0]
    assert entry["prompt_tokens"] is None
    assert entry["completion_tokens"] is None
    assert entry["total_tokens"] is None
    assert entry["estimated"] is True


def test_partial_usage_fields_are_not_marked_estimated_and_missing_side_counts_as_zero():
    client = _FakeStructuredClient(_structured_response(prompt_tokens=15, completion_tokens=None))

    _content, records = _run_structured(client)

    entry = records[0]
    assert entry["prompt_tokens"] == 15
    assert entry["completion_tokens"] is None
    assert entry["total_tokens"] == 15
    assert entry["estimated"] is False
