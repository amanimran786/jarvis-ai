"""
context_assembler.py — Priority ranking for Jarvis context blocks and conversation turns.

When the prompt budget is tight, compile_context_blocks() (context_budget.py) selects
blocks in priority order until the budget is exhausted. This module assigns those
priorities so the most valuable context survives compression.

Priority ladder (higher wins):
  99 — recent conversation turns (last RECENT_TURNS_PROTECTED pairs)
  98 — working memory (facts/prefs/projects from memory.json)
  96 — repeat context (recent code/task continuity signals)
  90 — vault knowledge (Obsidian KB)
  75 — graph context
  70 — semantic memory guidance hint
  65–95 — individual semantic memory hits, ranked by retrieval score
  55 — mem0 episodic cross-session memory
  10–40 — older conversation turns (decreasing with age)

Current query and system prompt are always injected by callers — not ranked here.

Pressure-aware trimming (see _rank_and_trim_context):
  ≥75%: semantic hits below 0.3 dropped; recent turns always protected.
  ≥90%: all episodic memory dropped; only semantic hits ≥0.5 survive;
         older conversation turns beyond the protected window are also dropped.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Most recent turn-pairs (user+assistant) protected from budget compression.
# rank_conversation_messages() places these at the end so they are dropped only
# after all older turns are exhausted.
RECENT_TURNS_PROTECTED = 3

# Score-to-priority mapping for individual semantic memory hits.
# Retrieval score ∈ [0.3, 1.0] → priority ∈ [65, 95] (linear).
# Hits below 0.3 are pre-filtered by semantic_memory.retrieve(min_score=0.3).
_SMEM_PRIORITY_LOW = 65
_SMEM_PRIORITY_HIGH = 95
_SMEM_SCORE_LOW = 0.3
_SMEM_SCORE_HIGH = 1.0

# Pressure thresholds that trigger trimming behaviour.
_PRESSURE_COMPRESS = 0.75
_PRESSURE_SWITCH = 0.90

# Minimum relevance scores per pressure tier.
_SMEM_MIN_SCORE_COMPRESS = 0.3   # ≥75%: drop hits below this
_SMEM_MIN_SCORE_SWITCH = 0.5     # ≥90%: drop hits below this


def _score_to_priority(score: float) -> int:
    clamped = max(_SMEM_SCORE_LOW, min(_SMEM_SCORE_HIGH, score))
    frac = (clamped - _SMEM_SCORE_LOW) / (_SMEM_SCORE_HIGH - _SMEM_SCORE_LOW)
    return int(_SMEM_PRIORITY_LOW + frac * (_SMEM_PRIORITY_HIGH - _SMEM_PRIORITY_LOW))


def _rank_and_trim_context(ctx_dict: dict, pressure: float) -> dict:
    """Filter and rank context components based on token-budget pressure.

    This is the single chokepoint for pressure-driven trimming. Callers should
    pass all mutable context fields here and use the returned dict downstream.

    Args:
        ctx_dict: Mapping of context fields. Recognised keys:
            smem_hits        — list[dict] of semantic memory hits; each hit must
                               carry a "score" float in [0.0, 1.0].
            conversation_turns — list[dict] of {role, content} messages (active
                               window, excluding the current query).
            mem0_ctx         — str: mem0 episodic cross-session blob (dropped
                               entirely at ≥90% pressure).
            episodic         — list[dict]: additional episodic blocks (dropped
                               entirely at ≥90% pressure).
            Unrecognised keys (query, system_prompt, working_mem, …) are passed
            through unchanged — they are always kept by callers.
        pressure: float in [0.0, 1.0] — ratio of context_used / context_budget.
                  Values below 0.75 cause an immediate no-op return.

    Returns:
        A new dict (shallow copy of ctx_dict) with lower-priority items removed:

        At ≥0.75 (compress):
          • smem_hits with score < 0.3 are dropped.
          • Surviving smem_hits are sorted highest-score-first.
          • Recent RECENT_TURNS_PROTECTED turn-pairs are always kept.

        At ≥0.90 (switch):
          • smem_hits with score < 0.5 are dropped (stricter than compress tier).
          • mem0_ctx is cleared ("").
          • episodic list is emptied ([]).
          • All conversation turns beyond the protected recent window are dropped.
    """
    if pressure < _PRESSURE_COMPRESS:
        return ctx_dict

    result = dict(ctx_dict)  # shallow copy — never mutate the caller's dict

    # ── Semantic memory hits ───────────────────────────────────────────────────
    smem_hits: list[dict] = list(result.get("smem_hits") or [])
    if pressure >= _PRESSURE_SWITCH:
        min_score = _SMEM_MIN_SCORE_SWITCH
    else:
        min_score = _SMEM_MIN_SCORE_COMPRESS

    smem_hits = sorted(
        [h for h in smem_hits if float(h.get("score", 0.0)) >= min_score],
        key=lambda h: float(h.get("score", 0.0)),
        reverse=True,
    )
    result["smem_hits"] = smem_hits

    # ── Episodic memory ────────────────────────────────────────────────────────
    if pressure >= _PRESSURE_SWITCH:
        result["mem0_ctx"] = ""
        result["episodic"] = []
        log.info(
            "[ContextAssembler] pressure=%.2f (≥%.2f): dropped all episodic, "
            "kept %d smem hits ≥%.1f",
            pressure, _PRESSURE_SWITCH, len(smem_hits), _SMEM_MIN_SCORE_SWITCH,
        )
    else:
        log.info(
            "[ContextAssembler] pressure=%.2f (≥%.2f): kept %d smem hits ≥%.1f",
            pressure, _PRESSURE_COMPRESS, len(smem_hits), _SMEM_MIN_SCORE_COMPRESS,
        )

    # ── Conversation turns ─────────────────────────────────────────────────────
    # Always protect the most recent RECENT_TURNS_PROTECTED user+assistant pairs.
    turns: list[dict] = list(result.get("conversation_turns") or [])
    if turns:
        protected_count = min(len(turns), RECENT_TURNS_PROTECTED * 2)
        recent = turns[-protected_count:]
        older = turns[:-protected_count] if protected_count < len(turns) else []

        if pressure >= _PRESSURE_SWITCH:
            # Drop all older turns; only the protected recent window survives.
            result["conversation_turns"] = recent
            if older:
                log.info(
                    "[ContextAssembler] pressure=%.2f (≥%.2f): dropped %d older turns, "
                    "kept %d recent",
                    pressure, _PRESSURE_SWITCH, len(older), len(recent),
                )
        # At compress tier (0.75–0.89), turns are not trimmed further — only
        # smem filtering applies; rank_conversation_messages() already puts
        # older turns at the front so compile_context_blocks() drops them last.

    return result


def rank_context_blocks(
    *,
    working_mem: str = "",
    repeat_ctx: str = "",
    vault_ctx: str = "",
    graph_ctx: str = "",
    semantic_hint: str = "",
    smem_hits: list[dict] | None = None,
    mem0_ctx: str = "",
    pressure: float = 0.0,
) -> list[dict]:
    """Return context blocks with priority scores for compile_context_blocks().

    Each semantic memory hit is a separate block ranked by its own retrieval
    score rather than sharing a flat priority with every other hit. This means
    a highly relevant memory (score 0.9 → priority 93) survives compression
    while a marginal one (score 0.35 → priority 67) does not.

    When pressure ≥ 0.75, _rank_and_trim_context() pre-filters smem_hits and
    episodic memory before blocks are assembled. This is the wiring point for
    the context-budget pressure checks (compress / switch tiers).
    """
    # Apply pressure-driven trimming before assembling blocks.
    if pressure >= _PRESSURE_COMPRESS:
        ctx = _rank_and_trim_context(
            {"smem_hits": list(smem_hits or []), "mem0_ctx": mem0_ctx},
            pressure,
        )
        smem_hits = ctx["smem_hits"]
        mem0_ctx = ctx.get("mem0_ctx", mem0_ctx)

    blocks: list[dict] = []

    if working_mem:
        blocks.append({"label": "working_memory", "content": working_mem, "priority": 98, "max_chars": 2000})

    if repeat_ctx:
        blocks.append({"label": "repeat_context", "content": repeat_ctx, "priority": 96, "max_chars": 1400})

    if vault_ctx:
        blocks.append({"label": "vault", "content": vault_ctx, "priority": 90, "max_chars": 2400})

    if graph_ctx:
        blocks.append({"label": "graph", "content": graph_ctx, "priority": 75, "max_chars": 1400})

    if semantic_hint:
        blocks.append({"label": "semantic_hint", "content": semantic_hint, "priority": 70, "max_chars": 700})

    for i, hit in enumerate(smem_hits or []):
        content = str(hit.get("content") or "").strip()
        if not content:
            continue
        score = float(hit.get("score", 0.5))
        priority = _score_to_priority(score)
        label = str(hit.get("label") or hit.get("id") or f"smem_{i}")
        blocks.append({
            "label": f"semantic:{label}",
            "content": content,
            "priority": priority,
            "max_chars": 600,
        })
        log.debug("[ContextAssembler] smem hit %s score=%.3f → priority=%d", label, score, priority)

    if mem0_ctx:
        blocks.append({"label": "mem0", "content": mem0_ctx, "priority": 55, "max_chars": 600})

    return blocks


def rank_conversation_messages(
    messages: list[dict],
    *,
    recent_protected: int = RECENT_TURNS_PROTECTED,
) -> list[dict]:
    """Order messages so budget-driven dropping preserves recent turns.

    Returns the message list with older turns first and the most recent
    `recent_protected` turn-pairs last. Callers that drop messages under
    token pressure should pop from the FRONT — this ensures recent context
    survives until all older turns are exhausted.

    The system message (index 0 in the Ollama messages list) and the current
    user request (always last) are managed by the caller and excluded here.
    """
    if not messages:
        return []
    protected_count = min(len(messages), recent_protected * 2)
    recent = messages[-protected_count:]
    older = messages[:-protected_count] if protected_count < len(messages) else []
    return list(older) + list(recent)
