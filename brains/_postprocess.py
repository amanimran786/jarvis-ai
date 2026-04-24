"""Shared text postprocessing utilities for Jarvis brain responses.

Centralised here so brain_ollama and brain_claude apply identical cleanup —
prevents reasoning blocks or markdown artefacts leaking into TTS/UI when a
brain other than the originally-tested one returns the response.
"""

from __future__ import annotations

import re


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think_blocks(text: str) -> str:
    """Remove DeepSeek R1 / reasoning-model <think>...</think> internal blocks.

    Safe to call on any text — leaves text without think tags untouched.
    """
    if not text or "<think>" not in text:
        return text
    text = _THINK_BLOCK.sub("", text)
    # Collapse the blank lines that the removed block leaves behind.
    text = re.sub(r"^\s*\n", "\n", text, flags=re.M)
    return text


def strip_markdown(text: str) -> str:
    """Remove markdown artefacts because Jarvis responses are spoken aloud.

    Mirrors the historical brain_ollama behaviour so cloud and local replies
    look identical to downstream TTS.
    """
    text = strip_think_blocks(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"^\s*#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*\d+[.)]\s*", "", text, flags=re.M)
    text = re.sub(r"(?<=\s)\d+[.)]\s*", " ", text)
    text = re.sub(r"```\w*\n?", "", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
