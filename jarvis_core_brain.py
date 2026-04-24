"""
jarvis_core_brain.py — Always-on identity and preference grounding for Jarvis.

Loads a compact (~1 000-char) snapshot of the four identity-tier brain notes
once at startup and keeps them in memory.  smart_stream injects this as the
first paragraph of every system prompt so every model — local or cloud — always
knows who Aman is, what Jarvis is for, and how to behave.

This replaces the per-query reactive vault search for identity facts.  Think of
it as what CLAUDE.md is for Codex: a permanent, always-present operating brief.

Vault notes used:
  vault/wiki/brain/10 Identity.md
  vault/wiki/brain/20 Projects.md
  vault/wiki/brain/30 Preferences.md
  vault/wiki/brain/80 Jarvis Roadmap.md
"""

from __future__ import annotations

import os
import threading
import time

_VAULT_ROOT = os.path.join(os.path.dirname(__file__), "vault", "wiki", "brain")

_BRAIN_FILES = {
    "identity":   "10 Identity.md",
    "projects":   "20 Projects.md",
    "preferences": "30 Preferences.md",
    "roadmap":    "80 Jarvis Roadmap.md",
}

# Headings (and content under them) to extract from each note.
# None means "read the whole note body up to the first section break".
_EXTRACT_SECTIONS = {
    "identity":   ["## Core Identity", "## Working Style", "## Current North Star"],
    "projects":   ["## Active Projects"],
    "preferences": ["## Communication", "## Product Taste", "## Engineering Preferences"],
    "roadmap":    ["## North Star", "## Non-Negotiables"],
}

_MAX_SECTION_CHARS = 400   # per note — keeps total under ~1 800 chars
_CACHE_TTL = 300.0          # refresh every 5 min in case notes change on disk

_lock = threading.Lock()
_cached: str = ""
_cached_at: float = 0.0


def _read_section(lines: list[str], heading: str, max_chars: int) -> str:
    """Extract lines under `heading` until the next same-level heading."""
    level = heading.count("#")
    marker = "#" * level + " "
    in_section = False
    buf: list[str] = []
    for line in lines:
        if line.startswith(heading):
            in_section = True
            continue
        if in_section:
            # Stop at any heading of the same or higher level
            if line.startswith(marker) and not line.startswith(heading):
                break
            buf.append(line)
    text = "\n".join(buf).strip()
    return text[:max_chars] if len(text) > max_chars else text


def _load_note(key: str) -> str:
    path = os.path.join(_VAULT_ROOT, _BRAIN_FILES[key])
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return ""

    lines = raw.splitlines()
    sections = _EXTRACT_SECTIONS.get(key, [])
    chunks: list[str] = []
    total = 0
    for heading in sections:
        text = _read_section(lines, heading, _MAX_SECTION_CHARS - total)
        if text:
            chunks.append(text)
            total += len(text)
        if total >= _MAX_SECTION_CHARS:
            break
    return "\n\n".join(chunks)


def _build_snapshot() -> str:
    parts: list[str] = []

    identity = _load_note("identity")
    if identity:
        parts.append(f"[User identity]\n{identity}")

    preferences = _load_note("preferences")
    if preferences:
        parts.append(f"[Preferences]\n{preferences}")

    projects = _load_note("projects")
    if projects:
        parts.append(f"[Active projects]\n{projects}")

    roadmap = _load_note("roadmap")
    if roadmap:
        parts.append(f"[Jarvis north star]\n{roadmap}")

    header = (
        "You are Jarvis — Aman's local-first personal AI runtime. "
        "The context below is always-on operating memory. "
        "Use it to stay grounded in who Aman is and what Jarvis is for:\n\n"
    )
    return header + "\n\n".join(parts)


def core_context() -> str:
    """Return the cached always-on brain snapshot, refreshing if stale."""
    global _cached, _cached_at
    now = time.monotonic()
    if _cached and (now - _cached_at) < _CACHE_TTL:
        return _cached
    with _lock:
        now = time.monotonic()
        if _cached and (now - _cached_at) < _CACHE_TTL:
            return _cached
        try:
            _cached = _build_snapshot()
        except Exception:
            # Never crash smart_stream — return empty on error
            _cached = ""
        _cached_at = time.monotonic()
    return _cached


def refresh() -> None:
    """Force a reload — call after vault writes that change identity notes."""
    global _cached_at
    with _lock:
        _cached_at = 0.0


# Pre-warm at import time (background thread so import doesn't block)
def _prewarm() -> None:
    try:
        core_context()
    except Exception:
        pass

threading.Thread(target=_prewarm, daemon=True, name="jarvis-brain-prewarm").start()
