"""Cheap repeat-request recall for Jarvis.

This is the first pass at the "do not burn tokens relearning the same lesson"
lane. It does not call a chat model. It looks at indexed vault notes, recent
conversation summaries, and mem0 episodic memory, then returns a tiny block the
router can inject before generation.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import OrderedDict
from typing import Any

_STOPWORDS = {
    "about", "again", "also", "and", "are", "can", "could", "did", "does",
    "for", "from", "have", "how", "into", "jarvis", "just", "like", "make",
    "need", "not", "now", "our", "please", "same", "that", "the", "this",
    "through", "want", "what", "when", "where", "which", "with", "would",
    "you", "your",
}
_REPEAT_MARKERS = (
    "again",
    "already",
    "before",
    "burn tokens",
    "chat history",
    "context window",
    "don't repeat",
    "same mistake",
    "same request",
    "save tokens",
    "seen this",
    "we talked about",
)
_CACHE_TTL_SECONDS = 60.0
_CACHE_SIZE = 64
_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()


def _tokens(text: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(token) > 2 and token not in _STOPWORDS
    }


def _fingerprint(query: str) -> str:
    tokens = sorted(_tokens(query))
    return hashlib.sha1(" ".join(tokens).encode("utf-8")).hexdigest()[:16]


def _trim(text: str, limit: int = 260) -> str:
    compact = re.sub(r"\s+", " ", (text or "").strip())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _score_overlap(query: str, text: str) -> float:
    left = _tokens(query)
    right = _tokens(text)
    if not left or not right:
        return 0.0
    return len(left & right) / max(len(left), 1)


def _is_repeat_shaped(query: str) -> bool:
    lower = (query or "").lower()
    if any(marker in lower for marker in _REPEAT_MARKERS):
        return True
    return len(_tokens(query)) >= 8


def _cached(key: str) -> dict[str, Any] | None:
    now = time.monotonic()
    hit = _cache.get(key)
    if not hit:
        return None
    created, payload = hit
    if now - created > _CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return payload


def _store_cache(key: str, payload: dict[str, Any]) -> dict[str, Any]:
    _cache[key] = (time.monotonic(), payload)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return payload


def _vault_hits(query: str) -> list[dict[str, Any]]:
    try:
        import vault

        searches = [
            query,
            f"{query} repeated lesson mistake correction",
            f"{query} context budget discipline local skill loop session lessons",
        ]
        hits: list[dict[str, Any]] = []
        seen: set[str] = set()
        for search in searches:
            for hit in vault.search(search, topn=3):
                path = str(hit.get("path") or "")
                heading = str(hit.get("matched_heading") or hit.get("title") or "")
                key = f"{path}:{heading}"
                if key in seen:
                    continue
                seen.add(key)
                score = float(hit.get("score") or 0)
                overlap = _score_overlap(query, f"{hit.get('title', '')} {hit.get('excerpt', '')}")
                if score < 6 and overlap < 0.18:
                    continue
                hits.append({
                    "source": "vault",
                    "score": score + (overlap * 20),
                    "label": (hit.get("citation") or {}).get("label") or path,
                    "text": hit.get("excerpt") or "",
                })
        return hits
    except Exception:
        return []


def _recent_summary_hits(query: str) -> list[dict[str, Any]]:
    try:
        import memory

        hits = []
        for convo in memory.get_recent_conversations(12):
            summary = str(convo.get("summary") or "")
            overlap = _score_overlap(query, summary)
            if overlap < 0.22:
                continue
            hits.append({
                "source": "conversation_summary",
                "score": overlap * 10,
                "label": f"memory conversation {convo.get('date', '')}".strip(),
                "text": summary,
            })
        return hits
    except Exception:
        return []


def _mem0_hits(query: str) -> list[dict[str, Any]]:
    if not _is_repeat_shaped(query):
        return []
    try:
        import mem0_layer

        hits = []
        for item in mem0_layer.search(query, top_k=3):
            text = str(item.get("memory") or "")
            if not text:
                continue
            score = float(item.get("score") or 0)
            overlap = _score_overlap(query, text)
            if score < 0.3 and overlap < 0.18:
                continue
            hits.append({
                "source": "mem0",
                "score": max(score * 10, overlap * 10),
                "label": "mem0 episodic memory",
                "text": text,
            })
        return hits
    except Exception:
        return []


def find(query: str, *, top_k: int = 4) -> dict[str, Any]:
    """Return ranked repeat-context hits without calling a chat model."""
    if not (query or "").strip():
        return {"fingerprint": "", "hits": [], "text": ""}
    key = _fingerprint(query)
    cached = _cached(key)
    if cached is not None:
        return cached

    hits = _vault_hits(query) + _recent_summary_hits(query) + _mem0_hits(query)
    hits.sort(key=lambda item: float(item.get("score") or 0), reverse=True)
    selected = hits[: max(1, int(top_k or 4))]

    lines = []
    for hit in selected:
        text = _trim(str(hit.get("text") or ""), 260)
        if not text:
            continue
        lines.append(f"- {hit.get('label')}: {text}")

    prompt_text = ""
    if lines:
        prompt_text = (
            "Seen-before context from local memory/vault. Use this to avoid "
            "relearning or repeating prior mistakes; treat it as supporting context and verify if stale:\n"
            + "\n".join(lines)
        )
    return _store_cache(key, {"fingerprint": key, "hits": selected, "text": prompt_text})


def context_for_prompt(query: str, *, max_chars: int = 1400) -> str:
    text = find(query).get("text", "")
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 16].rstrip() + "\n[truncated]"


def status() -> dict[str, Any]:
    return {
        "cache_size": len(_cache),
        "cache_ttl_seconds": _CACHE_TTL_SECONDS,
        "purpose": "cheap repeat-request recall from vault, recent summaries, and mem0",
    }
