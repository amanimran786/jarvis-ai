"""
Tests for the Kimi K2.7 candidate brain (brains/brain_kimi.py).

Focus: the safety contract (default OFF, isolated) and the streaming/usage path
under a mocked OpenRouter client. No real network calls.
"""

import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest


def _reload_brain_kimi(**config_overrides):
    """Reimport brain_kimi with patched config values so module-level client
    construction picks up the overrides (KIMI_ENABLED / key are read at import)."""
    import config

    with patch.multiple("config", **config_overrides):
        sys.modules.pop("brains.brain_kimi", None)
        return importlib.import_module("brains.brain_kimi")


def test_disabled_by_default_means_unavailable():
    # Arrange + Act: flag off, no client should be built even with a key present.
    mod = _reload_brain_kimi(KIMI_ENABLED=False, KIMI_API_KEY="sk-or-test")
    # Assert
    assert mod.is_available() is False


def test_enabled_without_key_is_unavailable():
    mod = _reload_brain_kimi(KIMI_ENABLED=True, KIMI_API_KEY=None)
    assert mod.is_available() is False


def test_ensure_client_message_when_disabled():
    mod = _reload_brain_kimi(KIMI_ENABLED=False, KIMI_API_KEY=None)
    with pytest.raises(RuntimeError, match="JARVIS_KIMI_ENABLED"):
        list(mod.ask_kimi_stream("write a qusort"))


def test_ensure_client_message_when_enabled_without_key():
    mod = _reload_brain_kimi(KIMI_ENABLED=True, KIMI_API_KEY=None)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        list(mod.ask_kimi_stream("write a qusort"))


def _fake_stream_chunks():
    def _chunk(content=None, usage=None):
        c = MagicMock()
        if content is None:
            c.choices = []
        else:
            delta = MagicMock()
            delta.content = content
            choice = MagicMock()
            choice.delta = delta
            c.choices = [choice]
        c.usage = usage
        return c

    usage = MagicMock(prompt_tokens=11, completion_tokens=7, total_tokens=18)
    yield _chunk("def f():")
    yield _chunk(" return 1")
    yield _chunk(content=None, usage=usage)  # final usage-only chunk


def test_stream_yields_raw_deltas_and_records_usage():
    # Arrange: enabled brain, but swap in a fake OpenAI-compatible client.
    mod = _reload_brain_kimi(KIMI_ENABLED=True, KIMI_API_KEY="sk-or-test")
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_stream_chunks()
    mod.client = fake_client

    with patch("brains.brain_kimi.usage_tracker.record") as rec:
        # Act
        out = "".join(mod.ask_kimi_stream("write f"))

    # Assert: code fences/markdown are NOT stripped (raw deltas), usage recorded.
    assert out == "def f(): return 1"
    rec.assert_called_once()
    kwargs = rec.call_args.kwargs
    assert kwargs["provider"] == "kimi"
    assert kwargs["prompt_tokens"] == 11
    assert kwargs["completion_tokens"] == 7
    assert kwargs["metadata"]["candidate"] is True


def test_ask_kimi_collects_stream():
    mod = _reload_brain_kimi(KIMI_ENABLED=True, KIMI_API_KEY="sk-or-test")
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_stream_chunks()
    mod.client = fake_client
    with patch("brains.brain_kimi.usage_tracker.record"):
        assert mod.ask_kimi("write f") == "def f(): return 1"


def teardown_module(module):
    # Restore a clean import for the rest of the suite.
    sys.modules.pop("brains.brain_kimi", None)
