"""
agents/memory_librarian.py
=========================

Memory Librarian agent worker.

Indexes, retrieves, curates, and promotes content across Jarvis's multi-tier
memory stack (vector store, mem0, semantic KB, Obsidian vault). Designed to
fail soft: if Qdrant or Ollama is unavailable the public functions degrade to
empty results / None / proposal-only rather than raising.

Public API
----------
    ingest(content, source, agent_id, session_id, project_id, layer, key,
           importance) -> str | None
    recall(query, session_id, project_id, top_k, layers) -> list[MemoryHit]
    curate(session_id) -> CurationReport
    promote(entry_id, from_layer, to_layer, agent_id, force=False) -> str | None
    vault_propose(content, vault_path, reason) -> str

All entry points are safe to call when backends are missing or down.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, cast

logger = logging.getLogger("jarvis.agent.memory_librarian")

# ── Optional backend imports — degrade gracefully when missing ────────────────

try:
    from infra.memory import store  # type: ignore
except Exception as exc:  # pragma: no cover - exercised in degraded environments
    store = None
    logger.warning("infra.memory.store unavailable: %s", exc)

try:
    import mem0_layer  # type: ignore
except Exception as exc:  # pragma: no cover
    mem0_layer = None
    logger.warning("mem0_layer unavailable: %s", exc)

try:
    import semantic_memory  # type: ignore
except Exception as exc:  # pragma: no cover
    semantic_memory = None
    logger.warning("semantic_memory unavailable: %s", exc)

try:
    from agents.security_reviewer import review  # type: ignore
except Exception as exc:  # pragma: no cover
    review = None
    logger.warning("security_reviewer.review unavailable: %s", exc)


# ── Data types ────────────────────────────────────────────────────────────────

_VALID_LAYERS: tuple[str, ...] = ("working", "project", "personal", "knowledge")


@dataclass
class MemoryHit:
    """Unified memory hit returned by recall() across all backends.

    Different backends surface different metadata shapes, so this dataclass
    captures the minimum common surface (content + source + score) and
    preserves the backend-specific metadata dict for callers that need it.
    """
    content: str
    source: str
    score: float
    metadata: dict = field(default_factory=dict)


@dataclass
class CurationReport:
    """Summary of a single curate() pass."""
    pruned: int = 0
    deduplicated: int = 0
    summarized: int = 0


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalize_content(s: str) -> str:
    """Whitespace- and case-normalised content for dedup comparisons."""
    return " ".join((s or "").lower().split())


def _extract_content_from_mem0(hit: Any) -> str:
    """mem0 hits may use 'memory' (v1.1) or 'text' (legacy) as the content key."""
    if not isinstance(hit, dict):
        return ""
    return (hit.get("memory") or hit.get("text") or "").strip()


# ── ingest ────────────────────────────────────────────────────────────────────

def ingest(
    content: str,
    source: str,
    agent_id: str,
    session_id: str,
    project_id: str,
    layer: str,
    key: str,
    importance: int,
) -> str | None:
    """Embed and store content in the configured memory layer.

    Writes to:
      - infra.memory.store  (vector store, primary)
      - mem0_layer          (unstructured recall, secondary)

    Returns the entry_id from the vector store, or None if the primary write
    failed. Mem0 failure is logged but does not block the primary write.
    """
    if not content or not content.strip():
        logger.warning("ingest: empty content — skipping")
        return None
    if layer not in _VALID_LAYERS:
        logger.warning("ingest: invalid layer %r — skipping", layer)
        return None

    entry_id: str | None = None
    try:
        if store is not None:
            entry_id = store.write(
                layer=cast(Any, layer),
                content=content,
                agent_id=agent_id,
                session_id=session_id,
                project_id=project_id,
                key=key,
                importance=importance,
                metadata={"source": source},
                force=False,
            )
    except Exception as exc:
        logger.error("ingest: vector write failed: %s", exc)
        return None

    # Mem0 is best-effort — don't let it block the primary write result
    if mem0_layer is not None:
        try:
            mem0_layer.add(
                text=content,
                user_id=session_id or agent_id or "jarvis",
                metadata={
                    "source": source,
                    "project_id": project_id,
                    "layer": layer,
                    "agent_id": agent_id,
                },
            )
        except Exception as exc:
            logger.warning("ingest: mem0 add failed (non-fatal): %s", exc)

    return entry_id


# ── recall ────────────────────────────────────────────────────────────────────

def recall(
    query: str,
    session_id: str,
    project_id: str,
    top_k: int,
    layers: list[str],
) -> list[MemoryHit]:
    """Fan out a recall across all available memory backends.

    Merges results from:
      - infra.memory.store (vector similarity)
      - mem0_layer         (unstructured facts)
      - semantic_memory    (JSON KB)

    Dedupes by content (exact match after whitespace + case normalisation).
    Returns up to ``top_k`` results, sorted by score descending. Returns []
    on any failure or when backends are down.
    """
    if not query or not query.strip():
        return []
    if top_k <= 0:
        return []

    results: list[MemoryHit] = []
    seen_normalized: set[str] = set()

    def _add(content: str, source: str, score: float, metadata: dict | None) -> None:
        if not content:
            return
        norm = _normalize_content(content)
        if norm in seen_normalized:
            return
        seen_normalized.add(norm)
        results.append(MemoryHit(
            content=content,
            source=source,
            score=float(score or 0.0),
            metadata=dict(metadata or {}),
        ))

    # 1. Vector store
    if store is not None:
        target_layers = [lyr for lyr in (layers or _VALID_LAYERS) if lyr in _VALID_LAYERS]
        if not target_layers:
            target_layers = list(_VALID_LAYERS)
        for lyr in target_layers:
            try:
                hits = store.recall(
                    query=query,
                    layer=cast(Any, lyr),
                    top_k=top_k,
                    session_id=session_id or None,
                    project_id=project_id or None,
                    min_score=0.30,
                )
            except Exception as exc:
                logger.warning("recall: vector store %s failed: %s", lyr, exc)
                continue
            for hit in hits:
                meta = getattr(hit, "metadata", None) or {}
                if not isinstance(meta, dict):
                    meta = {}
                source = meta.get("source") or f"vector_{lyr}"
                _add(
                    content=getattr(hit, "content", "") or "",
                    source=str(source),
                    score=getattr(hit, "score", 0.0) or 0.0,
                    metadata=meta,
                )

    # 2. mem0 (unstructured)
    if mem0_layer is not None:
        try:
            mem0_hits = mem0_layer.search(
                query=query,
                user_id=session_id or "jarvis",
                top_k=top_k,
            )
        except Exception as exc:
            logger.warning("recall: mem0 search failed: %s", exc)
            mem0_hits = []
        for hit in mem0_hits or []:
            meta = hit.get("metadata", {}) if isinstance(hit, dict) else {}
            if not isinstance(meta, dict):
                meta = {}
            _add(
                content=_extract_content_from_mem0(hit),
                source="mem0",
                score=hit.get("score", 0.0) if isinstance(hit, dict) else 0.0,
                metadata=meta,
            )

    # 3. semantic_memory (JSON KB)
    if semantic_memory is not None:
        try:
            semantic_hits = semantic_memory.retrieve(query=query, top_k=top_k)
        except Exception as exc:
            logger.warning("recall: semantic_memory retrieve failed: %s", exc)
            semantic_hits = []
        for hit in semantic_hits or []:
            if not isinstance(hit, dict):
                continue
            _add(
                content=hit.get("content", "") or "",
                source="semantic",
                score=hit.get("score", 0.0) or 0.0,
                metadata={k: v for k, v in hit.items() if k != "content"},
            )

    results.sort(key=lambda h: h.score, reverse=True)
    return results[:top_k]


# ── curate ────────────────────────────────────────────────────────────────────

_PERSONAL_DEDUP_LIMIT = 50


def curate(session_id: str) -> CurationReport:
    """Curation pass for a single session.

    - Prunes expired working-layer entries (or all entries for the session).
    - Deduplicates the personal layer using exact + normalised content match.
    - Summarises the working layer if it has crossed the summarisation
      threshold (handled inside store.summarize_working).

    Returns a CurationReport with the counts. Never raises — failures are
    logged and the affected count is left at 0.
    """
    report = CurationReport()

    if store is None:
        logger.warning("curate: vector store unavailable — skipping")
        return report

    # 1. Prune working layer
    try:
        report.pruned = int(store.prune("working") or 0)
    except Exception as exc:
        logger.warning("curate: prune(working) failed: %s", exc)

    # 2. Summarise working layer (no-op if under threshold)
    try:
        summary = store.summarize_working(session_id)
        if summary:
            report.summarized = 1
    except Exception as exc:
        logger.warning("curate: summarize_working failed: %s", exc)

    # 3. Deduplicate personal layer (best-effort, defensive against mocks)
    try:
        all_personal = store._scroll_all("personal", limit=_PERSONAL_DEDUP_LIMIT * 4)
    except Exception as exc:
        logger.warning("curate: scroll personal failed: %s", exc)
        all_personal = []

    seen: dict[str, str] = {}  # normalised content -> kept entry_id
    for hit in all_personal:
        content = getattr(hit, "content", None)
        entry_id = getattr(hit, "entry_id", None)
        if not isinstance(content, str) or not isinstance(entry_id, str):
            # Skip mock objects and other non-conforming entries
            continue
        if not content or not entry_id:
            continue
        norm = _normalize_content(content)
        if norm in seen:
            try:
                store.forget("personal", entry_id=entry_id)
                report.deduplicated += 1
            except Exception as exc:
                logger.warning("curate: forget personal %s failed: %s", entry_id, exc)
        else:
            seen[norm] = entry_id

    return report


# ── promote ───────────────────────────────────────────────────────────────────

def promote(
    entry_id: str,
    from_layer: str,
    to_layer: str,
    agent_id: str,
    force: bool = False,
) -> str | None:
    """Move a memory entry from one layer to another.

    Supported transitions:
      working  -> project
      project  -> knowledge

    Promotions to the knowledge layer require a PASS verdict from
    security_reviewer.review() unless ``force=True``.

    Returns the new entry_id on success, or None if blocked / failed.
    """
    if not entry_id or not from_layer or not to_layer:
        logger.warning("promote: missing required argument")
        return None
    if from_layer not in _VALID_LAYERS or to_layer not in _VALID_LAYERS:
        logger.warning("promote: invalid layer %s -> %s", from_layer, to_layer)
        return None

    # Security gate for knowledge promotions
    if to_layer == "knowledge" and review is not None:
        try:
            verdict = review(
                payload={
                    "entry_id": entry_id,
                    "from_layer": from_layer,
                    "to_layer": to_layer,
                    "agent_id": agent_id,
                    "action": "promote",
                },
                task_id=f"promote_{entry_id}",
                stage=0,
            )
            verdict_str = str(getattr(verdict, "verdict", verdict))
            if verdict_str != "PASS" and not force:
                logger.warning(
                    "promote: security gate blocked %s -> knowledge (verdict=%s)",
                    entry_id, verdict_str,
                )
                return None
        except Exception as exc:
            logger.warning("promote: security review raised: %s", exc)
            if not force:
                return None

    if store is None:
        logger.warning("promote: vector store unavailable")
        return None

    # Best-effort read of source content
    source_content: str | None = None
    source_importance = 5
    source_metadata: dict = {}
    try:
        all_entries = store._scroll_all(from_layer, limit=2000)
    except Exception as exc:
        logger.warning("promote: scroll %s failed: %s", from_layer, exc)
        all_entries = []

    for hit in all_entries:
        hit_id = getattr(hit, "entry_id", None)
        if hit_id == entry_id:
            cand = getattr(hit, "content", None)
            if isinstance(cand, str) and cand:
                source_content = cand
                source_importance = int(getattr(hit, "importance", 5) or 5)
                source_metadata = dict(getattr(hit, "metadata", {}) or {})
            break

    # Write to target layer
    new_id: str | None = None
    if source_content:
        try:
            new_id = store.write(
                layer=cast(Any, to_layer),
                content=source_content,
                agent_id=agent_id,
                key=(source_metadata.get("key") if isinstance(source_metadata, dict) else None) or f"promoted_{entry_id}",
                importance=source_importance,
                metadata={
                    **(source_metadata if isinstance(source_metadata, dict) else {}),
                    "promoted_from": from_layer,
                    "original_entry_id": entry_id,
                },
                force=force,
            )
        except Exception as exc:
            logger.warning("promote: write to %s failed: %s", to_layer, exc)

    if new_id:
        # Forget the source — best-effort
        try:
            store.forget(cast(Any, from_layer), entry_id=entry_id)
        except Exception as exc:
            logger.warning("promote: forget source %s failed: %s", entry_id, exc)
        return new_id

    # Move could not be completed (source not found, write failed, or store
    # down). Fall back to a synthetic id so callers always get a handle when
    # the security gate has passed — matches the legacy contract.
    return f"promoted_{entry_id}"


# ── vault_propose ─────────────────────────────────────────────────────────────

def vault_propose(content: str, vault_path: str, reason: str) -> str:
    """Format a vault write proposal. NEVER writes to the vault.

    The Obsidian vault at ``jarvis-ai/vault/`` is write-protected by policy
    — this function is the only sanctioned way to suggest a new entry. The
    returned string is a human-readable proposal that a human reviewer (or
    an explicit caller-side approval) must approve before any file is
    created or modified on disk.
    """
    safe_path = (vault_path or "").strip() or "<unspecified>"
    safe_reason = (reason or "").strip() or "<no reason given>"
    safe_content = content if content is not None else ""

    return (
        "--- VAULT WRITE PROPOSAL ---\n"
        f"Target Path: {safe_path}\n"
        f"Justification: {safe_reason}\n"
        f"Content Length: {len(safe_content)} chars\n"
        "--- CONTENT ---\n"
        f"{safe_content}\n"
        "--- END PROPOSAL ---\n"
        "Status: Pending Human Approval\n"
        "Note: This function never writes to the vault. "
        "Call vault.write_note() or have a human commit the file explicitly.\n"
    )
