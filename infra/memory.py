"""
Jarvis Four-Layer Memory System
--------------------------------
Four Qdrant collections, one per layer. Embeddings via brain_ollama.embed()
(nomic-embed-text, 768-dim). Pruning and summarization are policy-driven per layer.

Layers:
  working    — session-scoped, TTL 24h, max 50 entries. Pruned aggressively.
               Oldest entries summarized into a single "session_summary" when over limit.
  project    — project_id-scoped, no TTL. Pruned when importance < 3 after 30 days.
  personal   — user-scoped, no TTL, never auto-pruned. Near-duplicate dedup on write.
  knowledge  — global, write-protected after validation (requires force=True to overwrite).

Connection: tries Qdrant HTTP (localhost:6333 or QDRANT_HOST env) first,
falls back to on-disk at ~/.jarvis/memory/qdrant.

Usage:
    from infra.memory import store
    store.write("personal", "User prefers dark mode", agent_id="human")
    hits = store.recall("UI preferences", layer="personal", top_k=3)
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger("jarvis.memory")

Layer = Literal["working", "project", "personal", "knowledge"]

# ─── Layer policies ────────────────────────────────────────────────────────────

_LAYER_POLICIES: dict[str, dict] = {
    "working": {
        "ttl_seconds":      86400,     # 24h hard expiry
        "max_per_session":  50,        # trigger summarize+prune above this
        "summarize_batch":  15,        # how many old entries to collapse at once
        "auto_prune":       True,
    },
    "project": {
        "ttl_seconds":      None,      # no hard expiry
        "stale_days":       30,        # prune low-importance entries after 30 days
        "stale_importance": 3,         # importance threshold for stale pruning
        "auto_prune":       True,
    },
    "personal": {
        "ttl_seconds":      None,
        "dedup_threshold":  0.95,      # cosine score above which old entry is superseded
        "auto_prune":       False,
    },
    "knowledge": {
        "ttl_seconds":      None,
        "write_protected":  True,      # requires force=True to overwrite existing key
        "auto_prune":       False,
    },
}

_COLLECTION_PREFIX = "jarvis_mem_"
_EMBED_DIM = 768
_QDRANT_DISK_PATH = str(Path.home() / ".jarvis" / "memory" / "qdrant")


# ─── Result type ───────────────────────────────────────────────────────────────

@dataclass
class MemoryHit:
    entry_id:   str
    layer:      str
    content:    str
    score:      float
    agent_id:   str  = ""
    key:        str  = ""
    importance: int  = 5
    created_at: float = 0.0
    metadata:   dict = field(default_factory=dict)


# ─── Qdrant client factory ─────────────────────────────────────────────────────

_client_cache: Any = None

def _qdrant() -> Any:
    """
    Return a QdrantClient. Tries HTTP server first (docker-compose / production),
    falls back to on-disk embedded mode (local dev without Docker).
    """
    global _client_cache
    if _client_cache is not None:
        return _client_cache

    from qdrant_client import QdrantClient

    host = os.getenv("QDRANT_HOST", "localhost")
    port = int(os.getenv("QDRANT_PORT", "6333"))

    try:
        client = QdrantClient(host=host, port=port, timeout=3)
        client.get_collections()   # probe — raises if not reachable
        _client_cache = client
        log.info("Qdrant: connected to HTTP server %s:%d", host, port)
        return _client_cache
    except Exception:
        pass

    # Fall back to on-disk
    Path(_QDRANT_DISK_PATH).mkdir(parents=True, exist_ok=True)
    _client_cache = QdrantClient(path=_QDRANT_DISK_PATH)
    log.info("Qdrant: using on-disk store at %s", _QDRANT_DISK_PATH)
    return _client_cache


def _collection(layer: str) -> str:
    return f"{_COLLECTION_PREFIX}{layer}"


def _ensure_collections() -> None:
    from qdrant_client.models import Distance, VectorParams

    client = _qdrant()
    existing = {c.name for c in client.get_collections().collections}
    for layer in ("working", "project", "personal", "knowledge"):
        name = _collection(layer)
        if name not in existing:
            client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=_EMBED_DIM, distance=Distance.COSINE),
            )
            log.info("Created Qdrant collection '%s'", name)


# ─── Embedding ─────────────────────────────────────────────────────────────────

def _embed(text: str) -> list[float] | None:
    try:
        from brains.brain_ollama import embed
        return embed(text)
    except Exception as exc:
        log.warning("Embed failed: %s", exc)
        return None


# ─── Core store ────────────────────────────────────────────────────────────────

class MemoryStore:

    def __init__(self):
        self._ready = False

    def _init(self):
        if self._ready:
            return
        try:
            _ensure_collections()
            self._ready = True
        except Exception as exc:
            log.warning("MemoryStore init failed: %s — calls will be no-ops", exc)

    # ── Write ──────────────────────────────────────────────────────────────────

    def write(
        self,
        layer:      Layer,
        content:    str,
        agent_id:   str,
        *,
        session_id:  str | None = None,
        project_id:  str | None = None,
        key:         str | None = None,
        importance:  int = 5,
        metadata:    dict | None = None,
        force:       bool = False,
    ) -> str | None:
        """
        Embed and store a memory entry. Returns entry_id or None on failure.

        force=True is required to overwrite an existing key in the 'knowledge' layer.
        """
        self._init()
        if not self._ready:
            return None

        policy = _LAYER_POLICIES[layer]
        vec = _embed(content)
        if vec is None:
            log.warning("write(%s): embed returned None — skipping", layer)
            return None

        from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue

        # Knowledge layer: guard against overwriting unless force=True
        if layer == "knowledge" and key and not force:
            existing = self._scroll_by_key(layer, key)
            if existing:
                log.debug("knowledge write blocked: key='%s' exists (use force=True)", key)
                return existing[0].entry_id

        # Personal layer: dedup — supersede near-duplicate if score > threshold
        if layer == "personal":
            hits = self.recall(content, layer="personal", top_k=1)
            thresh = policy.get("dedup_threshold", 0.95)
            if hits and hits[0].score >= thresh:
                self._delete_entry(layer, hits[0].entry_id)
                log.debug("personal dedup: superseded entry_id=%s (score=%.3f)", hits[0].entry_id, hits[0].score)

        entry_id = str(uuid.uuid4())
        now = time.time()
        expires_at = (now + policy["ttl_seconds"]) if policy.get("ttl_seconds") else None

        payload: dict = {
            "layer":      layer,
            "content":    content,
            "agent_id":   agent_id,
            "key":        key or "",
            "importance": importance,
            "created_at": now,
            "expires_at": expires_at,
            "session_id": session_id or "",
            "project_id": project_id or "",
            **(metadata or {}),
        }

        _qdrant().upsert(
            collection_name=_collection(layer),
            points=[PointStruct(id=entry_id, vector=vec, payload=payload)],
        )

        # Working layer: check if we've hit the session limit
        if layer == "working" and session_id:
            self._maybe_summarize_working(session_id, agent_id)

        return entry_id

    # ── Recall ─────────────────────────────────────────────────────────────────

    def recall(
        self,
        query:      str,
        layer:      Layer | None = None,
        top_k:      int = 5,
        *,
        session_id:  str | None = None,
        project_id:  str | None = None,
        agent_id:    str | None = None,
        min_score:   float = 0.30,
    ) -> list[MemoryHit]:
        """
        Vector similarity search across one or all layers.
        Filters expired entries automatically.
        """
        self._init()
        if not self._ready:
            return []

        vec = _embed(query)
        if vec is None:
            return []

        from qdrant_client.models import Filter, FieldCondition, MatchValue

        layers = [layer] if layer else ["working", "project", "personal", "knowledge"]
        now = time.time()
        results: list[MemoryHit] = []

        for lyr in layers:
            conditions = []
            if session_id:
                conditions.append(FieldCondition(key="session_id", match=MatchValue(value=session_id)))
            if project_id:
                conditions.append(FieldCondition(key="project_id", match=MatchValue(value=project_id)))
            if agent_id:
                conditions.append(FieldCondition(key="agent_id", match=MatchValue(value=agent_id)))

            flt = Filter(must=conditions) if conditions else None

            try:
                resp = _qdrant().query_points(
                    collection_name=_collection(lyr),
                    query=vec,
                    limit=top_k * 2,   # over-fetch to allow TTL filtering
                    query_filter=flt,
                    with_payload=True,
                )
                hits = resp.points
            except Exception as exc:
                log.warning("recall(%s) search failed: %s", lyr, exc)
                continue

            for h in hits:
                if h.score < min_score:
                    continue
                p = h.payload or {}
                # Skip expired entries
                exp = p.get("expires_at")
                if exp and exp < now:
                    continue
                results.append(MemoryHit(
                    entry_id=str(h.id),
                    layer=lyr,
                    content=p.get("content", ""),
                    score=h.score,
                    agent_id=p.get("agent_id", ""),
                    key=p.get("key", ""),
                    importance=p.get("importance", 5),
                    created_at=p.get("created_at", 0.0),
                    metadata={k: v for k, v in p.items()
                              if k not in ("content", "agent_id", "key", "importance",
                                           "created_at", "expires_at", "layer",
                                           "session_id", "project_id")},
                ))

        results.sort(key=lambda h: h.score, reverse=True)
        return results[:top_k]

    # ── Forget ─────────────────────────────────────────────────────────────────

    def forget(
        self,
        layer:     Layer,
        *,
        entry_id:  str | None = None,
        key:       str | None = None,
        session_id: str | None = None,
    ) -> int:
        """Delete entries by id, key, or session. Returns count deleted."""
        self._init()
        if not self._ready:
            return 0

        deleted = 0
        if entry_id:
            self._delete_entry(layer, entry_id)
            return 1

        entries = []
        if key:
            entries = self._scroll_by_key(layer, key)
        elif session_id:
            entries = self._scroll_by_session(layer, session_id)

        for e in entries:
            self._delete_entry(layer, e.entry_id)
            deleted += 1
        return deleted

    # ── Prune ──────────────────────────────────────────────────────────────────

    def prune(self, layer: Layer, *, session_id: str | None = None) -> int:
        """
        Apply the layer's pruning policy. Returns count of removed entries.

        working  — removes expired entries; if session_id given, removes all entries for it
        project  — removes stale low-importance entries (age > 30 days, importance < 3)
        personal/knowledge — no-op (no auto-prune)
        """
        self._init()
        if not self._ready:
            return 0

        policy = _LAYER_POLICIES[layer]
        if not policy.get("auto_prune"):
            return 0

        now = time.time()
        removed = 0

        if layer == "working":
            if session_id:
                entries = self._scroll_by_session("working", session_id)
                for e in entries:
                    self._delete_entry("working", e.entry_id)
                    removed += 1
            else:
                # Prune all expired working entries
                all_entries = self._scroll_all("working", limit=2000)
                for e in all_entries:
                    exp = e.metadata.get("expires_at") or (e.created_at + 86400)
                    if exp < now:
                        self._delete_entry("working", e.entry_id)
                        removed += 1

        elif layer == "project":
            stale_days = policy.get("stale_days", 30)
            stale_imp  = policy.get("stale_importance", 3)
            cutoff = now - stale_days * 86400
            all_entries = self._scroll_all("project", limit=5000)
            for e in all_entries:
                if e.created_at < cutoff and e.importance < stale_imp:
                    self._delete_entry("project", e.entry_id)
                    removed += 1

        log.info("prune(%s): removed %d entries", layer, removed)
        return removed

    # ── Summarize working memory ────────────────────────────────────────────────

    def summarize_working(self, session_id: str, agent_id: str = "jarvis_manager") -> str | None:
        """
        Collapse the oldest SUMMARIZE_BATCH working entries for this session into a
        single "session_summary" entry. Deletes the source entries. Returns the summary
        text or None if nothing to summarize.
        """
        self._init()
        if not self._ready:
            return None

        policy = _LAYER_POLICIES["working"]
        batch_size = policy["summarize_batch"]

        entries = self._scroll_by_session("working", session_id)
        # Skip entries that are already summaries
        source = [e for e in entries if e.key != "session_summary"]
        if len(source) < batch_size:
            return None

        # Oldest first
        source.sort(key=lambda e: e.created_at)
        to_collapse = source[:batch_size]

        combined = "\n\n".join(
            f"[{i+1}] {e.content}" for i, e in enumerate(to_collapse)
        )
        prompt = (
            f"Summarize the following {batch_size} working memory entries from a single "
            f"AI assistant session into a compact paragraph that preserves all key facts, "
            f"decisions, and context. Output only the summary paragraph.\n\n{combined}"
        )

        try:
            from brains.brain_ollama import ask_local_stream
            summary_chunks = list(ask_local_stream(prompt))
            summary = "".join(summary_chunks).strip()
        except Exception as exc:
            log.warning("summarize_working: LLM call failed: %s", exc)
            summary = f"[Session summary of {batch_size} entries — LLM unavailable]"

        if not summary:
            return None

        # Write summary back as a high-importance working entry
        self.write(
            "working",
            summary,
            agent_id=agent_id,
            session_id=session_id,
            key="session_summary",
            importance=8,
        )

        # Delete the original entries
        for e in to_collapse:
            self._delete_entry("working", e.entry_id)

        log.info("summarize_working: collapsed %d entries → 1 summary (session=%s)", batch_size, session_id)
        return summary

    # ── Format for prompt injection ────────────────────────────────────────────

    def context_for_prompt(
        self,
        query:      str,
        top_k:      int = 5,
        layer:      Layer | None = None,
        session_id: str | None = None,
        project_id: str | None = None,
        max_chars:  int = 2000,
    ) -> str:
        """
        Retrieve relevant memories and format them as a context block suitable
        for injection into a system prompt.
        """
        hits = self.recall(
            query, layer=layer, top_k=top_k,
            session_id=session_id, project_id=project_id,
        )
        if not hits:
            return ""

        lines = ["[Memory context]"]
        total = 0
        for h in hits:
            tag  = f"[{h.layer}|score={h.score:.2f}]"
            line = f"{tag} {h.content}"
            if total + len(line) > max_chars:
                break
            lines.append(line)
            total += len(line)

        return "\n".join(lines)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _delete_entry(self, layer: str, entry_id: str) -> None:
        try:
            _qdrant().delete(
                collection_name=_collection(layer),
                points_selector=[entry_id],
            )
        except Exception as exc:
            log.warning("_delete_entry(%s, %s): %s", layer, entry_id, exc)

    def _scroll_by_key(self, layer: str, key: str) -> list[MemoryHit]:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        return self._scroll_filter(layer, Filter(must=[
            FieldCondition(key="key", match=MatchValue(value=key))
        ]))

    def _scroll_by_session(self, layer: str, session_id: str) -> list[MemoryHit]:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        return self._scroll_filter(layer, Filter(must=[
            FieldCondition(key="session_id", match=MatchValue(value=session_id))
        ]))

    def _scroll_filter(self, layer: str, flt, limit: int = 500) -> list[MemoryHit]:
        try:
            results, _ = _qdrant().scroll(
                collection_name=_collection(layer),
                scroll_filter=flt,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            log.warning("_scroll_filter(%s): %s", layer, exc)
            return []

        hits = []
        for r in results:
            p = r.payload or {}
            hits.append(MemoryHit(
                entry_id=str(r.id),
                layer=layer,
                content=p.get("content", ""),
                score=1.0,
                agent_id=p.get("agent_id", ""),
                key=p.get("key", ""),
                importance=p.get("importance", 5),
                created_at=p.get("created_at", 0.0),
                metadata=p,
            ))
        return hits

    def _scroll_all(self, layer: str, limit: int = 1000) -> list[MemoryHit]:
        try:
            results, _ = _qdrant().scroll(
                collection_name=_collection(layer),
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            log.warning("_scroll_all(%s): %s", layer, exc)
            return []

        return [
            MemoryHit(
                entry_id=str(r.id),
                layer=layer,
                content=(r.payload or {}).get("content", ""),
                score=1.0,
                agent_id=(r.payload or {}).get("agent_id", ""),
                key=(r.payload or {}).get("key", ""),
                importance=(r.payload or {}).get("importance", 5),
                created_at=(r.payload or {}).get("created_at", 0.0),
                metadata=r.payload or {},
            )
            for r in results
        ]

    def _maybe_summarize_working(self, session_id: str, agent_id: str) -> None:
        """Trigger summarization if the session has hit the entry limit."""
        policy = _LAYER_POLICIES["working"]
        entries = self._scroll_by_session("working", session_id)
        non_summary = [e for e in entries if e.key != "session_summary"]
        if len(non_summary) >= policy["max_per_session"]:
            self.summarize_working(session_id, agent_id)

    def status(self) -> dict:
        self._init()
        if not self._ready:
            return {"ready": False}
        out = {"ready": True, "collections": {}}
        for layer in ("working", "project", "personal", "knowledge"):
            try:
                info = _qdrant().get_collection(_collection(layer))
                out["collections"][layer] = {
                    "count": info.points_count,
                    "status": info.status.value if hasattr(info.status, "value") else str(info.status),
                }
            except Exception as exc:
                out["collections"][layer] = {"error": str(exc)}
        return out


# Module-level singleton
store = MemoryStore()
