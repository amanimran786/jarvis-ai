"""
harness/summarizer.py — Text summarization using best available local LLM.

Uses local Ollama models in priority order:
    1. ornith-9b
    2. qwen3:30b-a3b
    3. mistral:7b

Falls back to extractive summarization (first N sentences) when Ollama is
unavailable or the LLM response is unusably short.

Public API:
    summarize(text, sentences=3, model=None) -> str
    summarize_file(path, sentences=3) -> str
    get_best_summarizer_model() -> str
"""
from __future__ import annotations

import logging
import re
import subprocess
from typing import Optional

log = logging.getLogger(__name__)

# Models tried in priority order when no override is supplied.
_MODEL_PRIORITY = [
    "ornith-9b",
    "qwen3:30b-a3b",
    "mistral:7b",
]

_OLLAMA_TIMEOUT = 10  # seconds for `ollama list`


# ── Model selection ───────────────────────────────────────────────────────────

def get_best_summarizer_model() -> str:
    """Check ollama list and return the best available model for summarization.

    Priority: ornith-9b > qwen3:30b-a3b > mistral:7b
    Returns an empty string if Ollama is unreachable or none of the priority
    models are installed.
    """
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            timeout=_OLLAMA_TIMEOUT,
        )
        available = result.stdout.lower()
        for model in _MODEL_PRIORITY:
            if model.lower() in available:
                return model
    except Exception:
        log.debug("[Summarizer] ollama list failed", exc_info=True)
    return ""


def _pick_model(override: Optional[str] = None) -> Optional[str]:
    """Return the best available local model, or None if nothing is usable."""
    if override:
        return override
    m = get_best_summarizer_model()
    return m if m else None


# ── Extractive fallback ───────────────────────────────────────────────────────

def _extractive_summarize(text: str, sentences: int) -> str:
    """Return the first N sentences of text (naive sentence splitter)."""
    stripped = text.strip()
    if not stripped:
        return ""
    # Split on sentence-ending punctuation followed by whitespace or end-of-string.
    parts = re.split(r"(?<=[.!?])\s+", stripped)
    selected = [p for p in parts if p.strip()][:sentences]
    return " ".join(selected) if selected else stripped


# ── Public API ────────────────────────────────────────────────────────────────

def summarize(
    text: str,
    sentences: int = 3,
    model: Optional[str] = None,
) -> str:
    """Summarize *text* to *sentences* sentences using the best local LLM.

    Model priority: ornith-9b > qwen3:30b-a3b > mistral:7b
    Falls back to extractive summarization (first N sentences) if:
    - No Ollama model is available.
    - The LLM response is shorter than 10 characters.
    - ask_local() raises any exception.
    """
    if not text or not text.strip():
        return ""

    chosen = _pick_model(model)
    if chosen is None:
        log.warning("[Summarizer] no local model available — using extractive fallback")
        return _extractive_summarize(text, sentences)

    try:
        from brains.brain_ollama import ask_local

        prompt = (
            f"Summarize the following text in exactly {sentences} sentences. "
            "Capture the key points. No lists, no markdown, no preamble.\n\n"
            f"{text[:4000]}"
        )
        system = (
            f"You are a concise summarizer. Produce exactly {sentences} natural "
            "sentences. No bullet points, no headers, no markdown."
        )
        result = ask_local(
            prompt,
            model=chosen,
            system_extra=system,
            include_memory=False,
        )
        if result and len(result.strip()) > 10:
            return result.strip()

        log.debug("[Summarizer] LLM response too short — using extractive fallback")
        return _extractive_summarize(text, sentences)

    except Exception:
        log.debug("[Summarizer] ask_local failed — using extractive fallback", exc_info=True)
        return _extractive_summarize(text, sentences)


def summarize_file(path: str, sentences: int = 3) -> str:
    """Read the file at *path* and return a summarization of its contents."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        return f"Could not read file: {exc}"

    if not text.strip():
        return "File is empty."

    return summarize(text, sentences=sentences)
