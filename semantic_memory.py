"""
semantic_memory.py — TF-IDF semantic retrieval over memory/ JSON files.

Adds a structured, queryable knowledge layer on top of Jarvis's existing
memory.json store. Works standalone — no external dependencies beyond
scikit-learn (already in requirements for other modules).

Architecture:
    memory/semantic/public/        → facts safe for any model call
    memory/semantic/semi_private/  → facts safe for cloud (no raw PII)
    memory/episodic/professional/  → career events, time-indexed
    memory/episodic/technical/     → architecture decisions, build logs

JSON = persistent source of truth.
TF-IDF index = in-memory search layer rebuilt per process from JSON.

Usage:
    import semantic_memory as smem

    # Retrieve relevant context for a query
    hits = smem.retrieve(query="YouTube interview prep", top_k=3)
    context = smem.format_for_prompt(hits)

    # Write a new memory entry (index auto-invalidates)
    smem.write("semi_private", {
        "content": "Aman mentioned his interview is on April 10th.",
        "tags": ["interview", "date", "YouTube"],
    })

Integration points:
    - Call smem.retrieve() in model_router.smart_stream() to prepend
      relevant KB context before the model call.
    - Call smem.retrieve() in interview_profile to augment story context.
    - Call smem.write() from learner.py when extracting interview-related facts.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Paths ────────────────────────────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parent
MEMORY_DIR = _ROOT / "memory"
SEMANTIC_DIR = MEMORY_DIR / "semantic"
EPISODIC_DIR = MEMORY_DIR / "episodic"
CONVERSATIONS_DIR = MEMORY_DIR / "conversations"
VERBATIM_LOG_PATH = CONVERSATIONS_DIR / "verbatim.jsonl"

# ── TF-IDF index state ───────────────────────────────────────────────────────

_vectorizer = None
_matrix = None
_entries: list[dict[str, Any]] = []
_TIERS = ("public", "semi_private")

# ── Embedding index state (nomic-embed-text via Ollama) ──────────────────────
# When available, replaces TF-IDF with real semantic embeddings.
# Falls back to TF-IDF silently if Ollama embed isn't available.

_embed_vecs: list[list[float]] = []
_embed_ready: bool = False
_embed_matrix = None          # numpy matrix built once from _embed_vecs

# ── Query embedding LRU cache ────────────────────────────────────────────────
# Each Ollama embed() call costs ~10-50ms. Cache the last 64 query vectors so
# repeated or near-identical queries skip the roundtrip entirely.
_EMBED_CACHE_SIZE = 64
_embed_cache: OrderedDict[str, list[float]] = OrderedDict()
_embed_cache_lock = threading.Lock()


# ── Index management ─────────────────────────────────────────────────────────

def _load_all_entries() -> list[dict[str, Any]]:
    all_entries = []
    # Semantic tiers
    for tier in _TIERS:
        tier_dir = SEMANTIC_DIR / tier
        if not tier_dir.exists():
            continue
        for jf in sorted(tier_dir.glob("*.json")):
            try:
                raw = json.loads(jf.read_text(encoding="utf-8"))
                batch = raw if isinstance(raw, list) else [raw]
                for e in batch:
                    e.setdefault("_source", "semantic")
                    e.setdefault("_tier", tier)
                    all_entries.append(e)
            except Exception:
                continue
    # Episodic domains (keyword search only, lower weight)
    for domain_dir in sorted(EPISODIC_DIR.iterdir()) if EPISODIC_DIR.exists() else []:
        if not domain_dir.is_dir():
            continue
        for jf in sorted(domain_dir.glob("*.json")):
            try:
                e = json.loads(jf.read_text(encoding="utf-8"))
                e.setdefault("_source", "episodic")
                e.setdefault("_tier", "semi_private")
                all_entries.append(e)
            except Exception:
                continue
    # Verbatim conversation log
    if VERBATIM_LOG_PATH.exists():
        try:
            lines = VERBATIM_LOG_PATH.read_text(encoding="utf-8").splitlines()
        except Exception:
            lines = []
        max_conversation_entries = max(int(_safe_int_env("JARVIS_CONVERSATION_INDEX_LIMIT", 1200)), 100)
        for line in lines[-max_conversation_entries:]:
            line = (line or "").strip()
            if not line:
                continue
            try:
                convo = json.loads(line)
            except Exception:
                continue
            user_text = _compact_text(convo.get("user", ""))
            assistant_text = _compact_text(convo.get("assistant", ""))
            if not user_text and not assistant_text:
                continue
            content_parts = []
            if user_text:
                content_parts.append(f"User: {user_text}")
            if assistant_text:
                content_parts.append(f"Assistant: {assistant_text}")
            all_entries.append(
                {
                    "id": convo.get("id", _generate_id()),
                    "content": " | ".join(content_parts),
                    "tags": _conversation_tags(convo),
                    "_source": "conversation",
                    "_tier": "semi_private",
                    "created_at": convo.get("timestamp", ""),
                }
            )
    return all_entries


def _doc_text(e: dict[str, Any]) -> str:
    return f"{e.get('content', '')} {' '.join(e.get('tags', []))}"


def _safe_int_env(name: str, default: int) -> int:
    try:
        return int((os.getenv(name, str(default)) or str(default)).strip())
    except Exception:
        return default


def _compact_text(value: str, limit: int = 700) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _conversation_tags(entry: dict[str, Any]) -> list[str]:
    tags = ["conversation", "verbatim"]
    model = (entry.get("model") or "").strip()
    source = (entry.get("source") or "").strip()
    if model:
        tags.append(f"model:{model}")
    if source:
        tags.append(f"source:{source}")
    return tags


def _build_embed_index(entries: list[dict[str, Any]]) -> bool:
    """Try to build a real embedding index via Ollama. Returns True on success."""
    global _embed_vecs, _embed_ready, _embed_matrix
    try:
        from brains.brain_ollama import embed
        vecs = []
        for e in entries:
            v = embed(_doc_text(e))
            if v is None:
                return False
            vecs.append(v)
        _embed_vecs = vecs
        # Pre-build numpy matrix for O(1) batch cosine similarity
        try:
            import numpy as np
            mat = np.array(vecs, dtype=np.float32)
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            _embed_matrix = mat / norms   # unit-normalised rows
        except ImportError:
            _embed_matrix = None
        _embed_ready = True
        return True
    except Exception:
        return False


def _build_index() -> None:
    global _vectorizer, _matrix, _entries, _embed_vecs, _embed_ready
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
    except ImportError:
        _vectorizer = None
        return

    _entries = _load_all_entries()
    if not _entries:
        _vectorizer = None
        return

    # Try real embeddings first — better semantic recall
    if _build_embed_index(_entries):
        # Still build TF-IDF as fallback so _vectorizer exists
        pass

    docs = [_doc_text(e) for e in _entries]
    _vectorizer = TfidfVectorizer(
        ngram_range=(1, 2),
        min_df=1,
        sublinear_tf=True,
        strip_accents="unicode",
    )
    _matrix = _vectorizer.fit_transform(docs)


def _ensure_index() -> None:
    if _vectorizer is None or not _entries:
        _build_index()


def invalidate() -> None:
    """Force index rebuild on next retrieval. Call after writing new entries."""
    global _vectorizer, _matrix, _entries, _embed_vecs, _embed_ready, _embed_matrix
    _vectorizer = None
    _matrix = None
    _entries = []
    _embed_vecs = []
    _embed_ready = False
    _embed_matrix = None
    # Clear the query embedding LRU cache since the index is being rebuilt
    with _embed_cache_lock:
        _embed_cache.clear()


# ── Retrieval ────────────────────────────────────────────────────────────────

def _get_query_embedding(query: str) -> list[float] | None:
    """Return query embedding with LRU cache — avoids Ollama roundtrip on repeat queries."""
    with _embed_cache_lock:
        if query in _embed_cache:
            _embed_cache.move_to_end(query)
            return _embed_cache[query]
    try:
        from brains.brain_ollama import embed
        vec = embed(query)
    except Exception:
        return None
    if vec is None:
        return None
    with _embed_cache_lock:
        _embed_cache[query] = vec
        if len(_embed_cache) > _EMBED_CACHE_SIZE:
            _embed_cache.popitem(last=False)  # evict oldest
    return vec


def _scores_numpy(qvec: list[float], allowed_tiers: set) -> list[tuple[int, float]]:
    """Vectorized cosine similarity using numpy — O(n) with BLAS, not a Python loop."""
    import numpy as np
    q = np.array(qvec, dtype=np.float32)
    qn = np.linalg.norm(q)
    if qn == 0:
        return []
    q_unit = q / qn

    if _embed_matrix is not None:
        # _embed_matrix rows are already unit-normalised
        sims = _embed_matrix @ q_unit          # shape (n,)
    else:
        mat = np.array(_embed_vecs, dtype=np.float32)
        norms = np.linalg.norm(mat, axis=1)
        norms[norms == 0] = 1.0
        sims = (mat / norms[:, None]) @ q_unit

    results = []
    for i, score in enumerate(sims):
        if _entries[i].get("_tier") not in allowed_tiers:
            continue
        results.append((i, float(score)))
    return results


def retrieve(
    query: str,
    top_k: int = 5,
    min_score: float = 0.05,
    tiers: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Return top-k memory entries most relevant to query.

    Uses Ollama embeddings (nomic-embed-text) when available for true semantic
    similarity — query vectors are LRU-cached, document matrix is numpy-vectorized.
    Falls back to TF-IDF cosine similarity if embeddings aren't ready.
    """
    _ensure_index()
    allowed_tiers = set(tiers) if tiers else set(_TIERS)

    # ── Embedding path (preferred) ────────────────────────────────────────────
    if _embed_ready and _embed_vecs:
        try:
            qvec = _get_query_embedding(query)
            if qvec:
                try:
                    import numpy as _np
                    scored = _scores_numpy(qvec, allowed_tiers)
                except ImportError:
                    # Pure-Python fallback (no numpy)
                    import math
                    def _cos(a, b):
                        dot = sum(x * y for x, y in zip(a, b))
                        na = math.sqrt(sum(x*x for x in a))
                        nb = math.sqrt(sum(x*x for x in b))
                        return dot / (na * nb) if na and nb else 0.0
                    scored = [
                        (i, _cos(qvec, vec))
                        for i, vec in enumerate(_embed_vecs)
                        if _entries[i].get("_tier") in allowed_tiers
                    ]
                results = [
                    {**_entries[i], "score": round(sc, 4)}
                    for i, sc in scored
                    if sc >= min_score
                ]
                results.sort(key=lambda x: x["score"], reverse=True)
                return results[:top_k]
        except Exception:
            pass  # fall through to TF-IDF

    # ── TF-IDF fallback ───────────────────────────────────────────────────────
    if _vectorizer is None or _matrix is None:
        return []
    try:
        from sklearn.metrics.pairwise import cosine_similarity
        qvec = _vectorizer.transform([query])
        scores = cosine_similarity(qvec, _matrix)[0]
        results = []
        for i, score in enumerate(scores):
            if score < min_score:
                continue
            e = _entries[i]
            if e.get("_tier") not in allowed_tiers:
                continue
            results.append({**e, "score": round(float(score), 4)})
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]
    except Exception:
        return []


def retrieve_episodic_only(
    query: str,
    top_k: int = 3,
    min_score: float = 0.03,
) -> list[dict[str, Any]]:
    """Retrieve only from episodic entries."""
    _ensure_index()
    if _vectorizer is None or _matrix is None:
        return []
    try:
        from sklearn.metrics.pairwise import cosine_similarity
        results = []
        qvec = _vectorizer.transform([query])
        scores = cosine_similarity(qvec, _matrix)[0]
        for i, score in enumerate(scores):
            if score < min_score:
                continue
            if _entries[i].get("_source") != "episodic":
                continue
            results.append({**_entries[i], "score": round(float(score), 4)})
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]
    except Exception:
        return []


# ── Writing ──────────────────────────────────────────────────────────────────

def write(tier: str, entry: dict[str, Any]) -> Path:
    """
    Write a new semantic memory entry to JSON.
    Invalidates the TF-IDF index so the new entry appears on next retrieval.
    tier: "public" | "semi_private"
    """
    entry = dict(entry)
    entry.setdefault("id", _generate_id())
    entry.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    entry.setdefault("use_count", 0)
    entry.setdefault("tags", [])
    entry["privacy_tier"] = tier

    out_dir = SEMANTIC_DIR / tier
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{entry['id']}.json"
    path.write_text(json.dumps(entry, indent=2, ensure_ascii=False))

    invalidate()
    return path


def write_episodic(domain: str, event: dict[str, Any]) -> Path:
    """
    Write a new episodic memory entry.
    domain: "professional" | "technical"
    """
    event = dict(event)
    event.setdefault("id", _generate_id())
    event.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    event.setdefault("domain", domain)
    event.setdefault("tags", [])

    out_dir = EPISODIC_DIR / domain
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{event['id']}.json"
    path.write_text(json.dumps(event, indent=2, ensure_ascii=False))

    invalidate()
    return path


def log_conversation_turn(user_input: str, assistant_response: str, model: str = "", source: str = "") -> None:
    """Append a verbatim turn to local conversation memory and invalidate index."""
    user_text = _compact_text(user_input, limit=2000)
    assistant_text = _compact_text(assistant_response, limit=2500)
    if not user_text and not assistant_text:
        return

    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "id": _generate_id(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user": user_text,
        "assistant": assistant_text,
        "model": (model or "").strip(),
        "source": (source or "").strip(),
    }
    with VERBATIM_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    invalidate()


# ── Formatting ───────────────────────────────────────────────────────────────

def format_for_prompt(hits: list[dict[str, Any]], max_chars: int = 2000) -> str:
    """
    Format retrieved entries for injection into a model prompt.
    Returns empty string if no hits.
    """
    if not hits:
        return ""

    lines = ["[Relevant context from Jarvis knowledge base]"]
    total = len(lines[0])

    for h in hits:
        content = h.get("content", "").strip()
        if not content:
            continue
        score = h.get("score", 0)
        snippet = f"• [{score:.2f}] {content}"
        if total + len(snippet) > max_chars:
            break
        lines.append(snippet)
        total += len(snippet)

    return "\n".join(lines) if len(lines) > 1 else ""


def context_for_query(query: str, top_k: int = 4, max_chars: int = 1800) -> str:
    """
    One-call helper: retrieve + format.
    Returns empty string if nothing relevant found.
    """
    hits = retrieve(query, top_k=top_k)
    return format_for_prompt(hits, max_chars=max_chars)


# ── Utility ──────────────────────────────────────────────────────────────────

def _generate_id() -> str:
    import hashlib, time
    return hashlib.md5(str(time.time_ns()).encode()).hexdigest()[:12]


def status() -> dict[str, Any]:
    """Return current index state — useful for debugging."""
    _ensure_index()
    return {
        "entries_indexed": len(_entries),
        "semantic_entries": sum(1 for e in _entries if e.get("_source") == "semantic"),
        "episodic_entries": sum(1 for e in _entries if e.get("_source") == "episodic"),
        "conversation_entries": sum(1 for e in _entries if e.get("_source") == "conversation"),
        "index_ready": _vectorizer is not None,
        "retrieval_backend": "ollama-embeddings" if _embed_ready else "tfidf",
        "memory_dir": str(MEMORY_DIR),
        "conversation_log": str(VERBATIM_LOG_PATH),
    }
