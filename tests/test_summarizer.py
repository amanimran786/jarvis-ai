"""
tests/test_summarizer.py — 10 tests for harness/summarizer.py.

All subprocess and LLM calls are mocked — no real Ollama or IO during the suite.

Coverage:
  - Normal LLM path (mock ask_local)
  - Extractive fallback when Ollama is down / no model
  - summarize_file() reads file and calls summarize()
  - Model priority order (ornith-9b > qwen3:30b-a3b > mistral:7b)
  - Empty text edge case → returns ""
  - model= override bypasses priority list
  - Short LLM response → extractive fallback
  - ask_local exception → extractive fallback
  - File not found → error string
  - Empty file → "empty" message
"""
from __future__ import annotations

import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

# ── Sandbox stubs ─────────────────────────────────────────────────────────────
# `ollama` and `brains.brain_ollama` aren't installed in the CI/sandbox
# environment. Pre-stub them so patch() can target brains.brain_ollama.*
# without an ImportError.
#
# patch() resolves "brains.brain_ollama.ask_local" via:
#   1. __import__("brains.brain_ollama") — works once sys.modules has the entry
#   2. getattr(brains_pkg, "brain_ollama") — needs the attr on the package obj
# Both conditions must be satisfied.
if "ollama" not in sys.modules:
    sys.modules["ollama"] = MagicMock()
if "brains.brain_ollama" not in sys.modules:
    _brain_stub = MagicMock()
    sys.modules["brains.brain_ollama"] = _brain_stub
    import brains as _brains_pkg
    _brains_pkg.brain_ollama = _brain_stub

from harness import summarizer as sz


# ── Helpers ───────────────────────────────────────────────────────────────────

LONG_TEXT = (
    "The quick brown fox jumps over the lazy dog. "
    "Meanwhile the dog contemplated its life choices. "
    "In the end, the fox and the dog became unlikely friends. "
    "They lived happily ever after in the forest."
)


def _mock_ollama_list(*model_names: str) -> MagicMock:
    """Return a subprocess.CompletedProcess-like mock whose .stdout lists model_names."""
    proc = MagicMock()
    proc.stdout = "\n".join(model_names) + "\n"
    return proc


# ── 1. Normal LLM path ───────────────────────────────────────────────────────

class TestSummarizeLLMPath:
    def test_returns_llm_summary_when_model_available(self):
        """Happy path: model found, ask_local returns usable text."""
        with patch("harness.summarizer.subprocess.run",
                   return_value=_mock_ollama_list("ornith-9b")), \
             patch("brains.brain_ollama.ask_local", return_value="A concise summary here."):
            out = sz.summarize(LONG_TEXT, sentences=2)
        assert out == "A concise summary here."

    def test_model_override_bypasses_priority_list(self):
        """Explicit model= kwarg is forwarded directly to ask_local, not auto-selected."""
        captured: list[str] = []

        def _capture(prompt, model, **kw):
            captured.append(model)
            return "Override model produced this."

        with patch("harness.summarizer.subprocess.run",
                   return_value=_mock_ollama_list("ornith-9b")), \
             patch("brains.brain_ollama.ask_local", side_effect=_capture):
            sz.summarize(LONG_TEXT, model="custom-model:1b")

        assert captured == ["custom-model:1b"]


# ── 2. Extractive fallback ───────────────────────────────────────────────────

class TestExtractiveFallback:
    def test_fallback_when_ollama_unreachable(self):
        """subprocess.run raises → no LLM available → first-N-sentences returned."""
        with patch("harness.summarizer.subprocess.run",
                   side_effect=FileNotFoundError("ollama not found")):
            out = sz.summarize(LONG_TEXT, sentences=1)
        assert "quick brown fox" in out

    def test_fallback_when_no_priority_model_installed(self):
        """Ollama is reachable but none of the priority models are pulled."""
        with patch("harness.summarizer.subprocess.run",
                   return_value=_mock_ollama_list("llama3.1:8b")):
            out = sz.summarize(LONG_TEXT, sentences=2)
        assert "quick brown fox" in out

    def test_fallback_when_llm_response_too_short(self):
        """LLM returns ≤ 10 chars → fall back to extractive."""
        with patch("harness.summarizer.subprocess.run",
                   return_value=_mock_ollama_list("mistral:7b")), \
             patch("brains.brain_ollama.ask_local", return_value="ok"):
            out = sz.summarize(LONG_TEXT, sentences=2)
        assert "quick brown fox" in out

    def test_fallback_on_ask_local_exception(self):
        """ask_local() raises → extractive fallback, no exception propagated to caller."""
        with patch("harness.summarizer.subprocess.run",
                   return_value=_mock_ollama_list("mistral:7b")), \
             patch("brains.brain_ollama.ask_local", side_effect=RuntimeError("Ollama offline")):
            out = sz.summarize(LONG_TEXT, sentences=2)
        assert "quick brown fox" in out

    def test_empty_text_returns_empty_string(self):
        """summarize('') → '' without calling any LLM or subprocess."""
        with patch("harness.summarizer.subprocess.run") as mock_sub:
            out = sz.summarize("")
        assert out == ""
        mock_sub.assert_not_called()

        with patch("harness.summarizer.subprocess.run") as mock_sub2:
            out2 = sz.summarize("   \n\t  ")
        assert out2 == ""
        mock_sub2.assert_not_called()


# ── 3. Model priority ────────────────────────────────────────────────────────

class TestModelPriority:
    def test_ornith_preferred_over_all(self):
        with patch("harness.summarizer.subprocess.run",
                   return_value=_mock_ollama_list("ornith-9b", "qwen3:30b-a3b", "mistral:7b")):
            assert sz.get_best_summarizer_model() == "ornith-9b"

    def test_qwen_preferred_when_ornith_absent(self):
        with patch("harness.summarizer.subprocess.run",
                   return_value=_mock_ollama_list("qwen3:30b-a3b", "mistral:7b")):
            assert sz.get_best_summarizer_model() == "qwen3:30b-a3b"

    def test_mistral_selected_as_last_resort(self):
        with patch("harness.summarizer.subprocess.run",
                   return_value=_mock_ollama_list("mistral:7b")):
            assert sz.get_best_summarizer_model() == "mistral:7b"

    def test_empty_string_when_no_priority_model_present(self):
        with patch("harness.summarizer.subprocess.run",
                   return_value=_mock_ollama_list("llama3.1:8b", "phi3:mini")):
            assert sz.get_best_summarizer_model() == ""


# ── 4. summarize_file() ──────────────────────────────────────────────────────

class TestSummarizeFile:
    def test_reads_file_and_delegates_to_summarize(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(LONG_TEXT)
            path = fh.name

        try:
            with patch("harness.summarizer.subprocess.run",
                       return_value=_mock_ollama_list("ornith-9b")), \
                 patch("brains.brain_ollama.ask_local", return_value="File summary result."):
                out = sz.summarize_file(path)
            assert out == "File summary result."
        finally:
            os.unlink(path)

    def test_nonexistent_file_returns_error_string(self):
        out = sz.summarize_file("/definitely/missing/path/no_file.txt")
        assert "Could not read file" in out

    def test_empty_file_returns_empty_message(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("   \n  \t  ")   # whitespace only
            path = fh.name
        try:
            out = sz.summarize_file(path)
            assert "empty" in out.lower()
        finally:
            os.unlink(path)
