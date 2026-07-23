"""Exact Ollama model-reference matching shared by local routing paths."""

from __future__ import annotations

from collections.abc import Iterable


def normalize_ollama_model_ref(model: str) -> str:
    """Normalize case and Ollama's implicit ``:latest`` tag only."""
    value = str(model or "").strip().lower()
    return value[:-7] if value.endswith(":latest") else value


def ollama_model_refs_match(expected: str, available: str) -> bool:
    """Return True only when two references identify the same Ollama model."""
    requested = normalize_ollama_model_ref(expected)
    return bool(requested) and requested == normalize_ollama_model_ref(available)


def find_exact_ollama_model(
    expected: str,
    available_models: Iterable[str],
) -> str | None:
    """Return the installed reference matching ``expected``, preserving its tag."""
    for available in available_models:
        installed = str(available).strip()
        if ollama_model_refs_match(expected, installed):
            return installed
    return None
