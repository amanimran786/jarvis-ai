"""
mem0_layer.py — mem0 memory integration for Jarvis (local-first, Ollama-backed).

mem0 gives Jarvis a structured memory tier that persists conversation facts,
decisions, and preferences across sessions — the same mechanism that makes
Claude Projects feel like they remember you.

Architecture
────────────
  Tier 1: mem0 (Ollama-embedded, Qdrant local vector store)
           → stores conversation facts and Jarvis-observed preferences
           → survives restarts, queryable semantically
  Tier 2: semantic_memory.py (existing TF-IDF/Ollama embed over JSON files)
           → curated semantic KB — interview stories, professional facts
  Tier 3: vault.py (Obsidian brain notes)
           → long-form durable knowledge

This module wraps mem0 as Tier 1.  It degrades gracefully when Ollama is
unavailable — all calls return empty results rather than crashing.

Local storage
─────────────
  Vector store:  ~/.mem0/jarvis/qdrant/     (Qdrant on-disk)
  History DB:    ~/.mem0/jarvis/history.db  (SQLite for mem0 provenance)

Ollama models used
──────────────────
  Embedder: nomic-embed-text:latest   (768-dim, fast, already used by semantic_memory)
  LLM:      jarvis-local:latest       (extraction/dedup — uses LOCAL_TUNED from config)

Usage from model_router
───────────────────────
  import mem0_layer as _m0
  hits = _m0.search(query, user_id="aman", top_k=3)
  context = _m0.format_for_prompt(hits)

Usage from router (write path)
────────────────────────────────
  import mem0_layer as _m0
  _m0.add(conversation_turn, user_id="aman")   # async-safe, fire-and-forget
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

# ── Storage paths ──────────────────────────────────────────────────────────────
_HOME = Path.home()
_MEM0_DIR = _HOME / ".mem0" / "jarvis"
_QDRANT_PATH = str(_MEM0_DIR / "qdrant")
_HISTORY_PATH = str(_MEM0_DIR / "history.db")

_MEM0_DIR.mkdir(parents=True, exist_ok=True)

# ── Config ─────────────────────────────────────────────────────────────────────
_OLLAMA_BASE = os.getenv("OLLAMA_HOST", "http://localhost:11434")
_DEFAULT_USER = "aman"

# ── Lazy singleton ─────────────────────────────────────────────────────────────
_memory_instance: Any = None
_init_lock = threading.Lock()
_init_attempted = False
_available = False   # set True only after successful init


def _build_config() -> dict:
    """Build mem0 config using local Ollama models — no cloud required."""
    # Import here to read current config values
    try:
        from config import LOCAL_TUNED, LOCAL_DEFAULT
        llm_model = LOCAL_TUNED or LOCAL_DEFAULT or "gemma4:e4b"
    except Exception:
        llm_model = "gemma4:e4b"

    return {
        "llm": {
            "provider": "ollama",
            "config": {
                "model": llm_model,
                "ollama_base_url": _OLLAMA_BASE,
                "temperature": 0,
                "max_tokens": 512,
            },
        },
        "embedder": {
            "provider": "ollama",
            "config": {
                "model": "nomic-embed-text:latest",
                "ollama_base_url": _OLLAMA_BASE,
            },
        },
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": "jarvis",
                "path": _QDRANT_PATH,
            },
        },
        "history_db_path": _HISTORY_PATH,
        "version": "v1.1",
    }


def _get_instance() -> Any | None:
    """Return the mem0 Memory singleton, initialising on first call."""
    global _memory_instance, _init_attempted, _available
    if _init_attempted:
        return _memory_instance
    with _init_lock:
        if _init_attempted:
            return _memory_instance
        _init_attempted = True
        try:
            from mem0 import Memory
            _memory_instance = Memory.from_config(_build_config())
            _available = True
        except Exception as e:
            # Ollama not running, qdrant not installed, etc. — degrade silently.
            _memory_instance = None
            _available = False
    return _memory_instance


def is_available() -> bool:
    """True if mem0 initialized successfully."""
    _get_instance()  # trigger lazy init
    return _available


# ── Write path ─────────────────────────────────────────────────────────────────

def add(text: str, user_id: str = _DEFAULT_USER, metadata: dict | None = None) -> bool:
    """
    Store a conversation turn or fact in mem0.

    Safe to call fire-and-forget — returns False on any error.
    Blocks for ~200-800ms on first Ollama embed call; subsequent calls are faster.
    """
    if not text or not text.strip():
        return False
    m = _get_instance()
    if m is None:
        return False
    try:
        m.add(text.strip(), user_id=user_id, metadata=metadata or {})
        return True
    except Exception:
        return False


def add_async(text: str, user_id: str = _DEFAULT_USER, metadata: dict | None = None) -> None:
    """Fire-and-forget async wrapper — won't block the response stream."""
    threading.Thread(
        target=add,
        args=(text, user_id),
        kwargs={"metadata": metadata},
        daemon=True,
        name="mem0-write",
    ).start()


# ── Read path ──────────────────────────────────────────────────────────────────

def search(query: str, user_id: str = _DEFAULT_USER, top_k: int = 5) -> list[dict]:
    """
    Retrieve relevant memories for a query.

    Returns a list of dicts with at least {"memory": str, "score": float}.
    Returns [] on any error or when unavailable.
    """
    if not query or not query.strip():
        return []
    m = _get_instance()
    if m is None:
        return []
    try:
        results = m.search(query.strip(), user_id=user_id, limit=top_k)
        # mem0 returns {"results": [...]} in v1.1
        if isinstance(results, dict):
            return results.get("results", [])
        if isinstance(results, list):
            return results
        return []
    except Exception:
        return []


def get_all(user_id: str = _DEFAULT_USER) -> list[dict]:
    """Return all stored memories for a user (use sparingly — can be large)."""
    m = _get_instance()
    if m is None:
        return []
    try:
        results = m.get_all(user_id=user_id)
        if isinstance(results, dict):
            return results.get("results", [])
        if isinstance(results, list):
            return results
        return []
    except Exception:
        return []


def delete_all(user_id: str = _DEFAULT_USER) -> bool:
    """Wipe all memories for a user — use only on explicit request."""
    m = _get_instance()
    if m is None:
        return False
    try:
        m.delete_all(user_id=user_id)
        return True
    except Exception:
        return False


# ── Format for prompt ──────────────────────────────────────────────────────────

def format_for_prompt(hits: list[dict], max_chars: int = 600) -> str:
    """Convert mem0 search results into a concise system prompt snippet."""
    if not hits:
        return ""
    lines: list[str] = ["[Remembered context from past sessions]"]
    total = 0
    for hit in hits:
        mem = (hit.get("memory") or "").strip()
        if not mem:
            continue
        score = hit.get("score", 0.0)
        if score < 0.3:   # skip low-confidence hits
            continue
        line = f"  • {mem}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    if len(lines) <= 1:
        return ""
    return "\n".join(lines)


# ── Status ─────────────────────────────────────────────────────────────────────

def status() -> dict:
    """Return a status dict for /memory-status and similar commands."""
    avail = is_available()
    info: dict = {
        "available": avail,
        "store": _QDRANT_PATH if avail else "not initialized",
        "history_db": _HISTORY_PATH if avail else "not initialized",
    }
    if avail:
        try:
            all_mems = get_all()
            info["count"] = len(all_mems)
        except Exception:
            info["count"] = "unknown"
    return info
