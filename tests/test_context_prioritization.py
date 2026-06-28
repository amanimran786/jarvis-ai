"""
tests/test_context_prioritization.py — Relevance-ranked context trimming under pressure.

Covers _rank_and_trim_context() and the pressure param wired into rank_context_blocks().
"""
from __future__ import annotations

import pytest

import context_assembler as ca
from context_assembler import (
    _rank_and_trim_context,
    rank_context_blocks,
    RECENT_TURNS_PROTECTED,
    _PRESSURE_COMPRESS,
    _PRESSURE_SWITCH,
    _SMEM_MIN_SCORE_COMPRESS,
    _SMEM_MIN_SCORE_SWITCH,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _hit(score: float, label: str = "", content: str = "some content") -> dict:
    return {"score": score, "label": label or f"hit_{score}", "content": content}


def _turn(role: str = "user", content: str = "hello") -> dict:
    return {"role": role, "content": content}


def _turns(n: int) -> list[dict]:
    """Build a flat list of n user+assistant message dicts (n pairs = 2n messages)."""
    result = []
    for i in range(n):
        result.append(_turn("user", f"user msg {i}"))
        result.append(_turn("assistant", f"assistant msg {i}"))
    return result


# ── 1. No trimming below 75% ───────────────────────────────────────────────────

class TestNoPressure:
    def test_returns_same_dict_below_threshold(self):
        ctx = {
            "smem_hits": [_hit(0.1), _hit(0.2)],
            "mem0_ctx": "episodic blob",
            "conversation_turns": _turns(5),
            "query": "hello",
        }
        result = _rank_and_trim_context(ctx, pressure=0.5)
        assert result is ctx  # identity — no copy made

    def test_exact_below_threshold(self):
        ctx = {"smem_hits": [_hit(0.1)], "mem0_ctx": "keep me"}
        result = _rank_and_trim_context(ctx, pressure=0.74)
        assert result["mem0_ctx"] == "keep me"
        assert len(result["smem_hits"]) == 1

    def test_zero_pressure_no_op(self):
        ctx = {"smem_hits": [_hit(0.0)], "mem0_ctx": "x"}
        assert _rank_and_trim_context(ctx, pressure=0.0) is ctx


# ── 2. Compress tier (0.75 ≤ p < 0.90) ───────────────────────────────────────

class TestCompressTier:
    def test_smem_below_0_3_dropped(self):
        hits = [_hit(0.1), _hit(0.25), _hit(0.3), _hit(0.6)]
        ctx = {"smem_hits": hits}
        result = _rank_and_trim_context(ctx, pressure=0.75)
        scores = [h["score"] for h in result["smem_hits"]]
        assert 0.1 not in scores
        assert 0.25 not in scores
        assert 0.3 in scores
        assert 0.6 in scores

    def test_smem_hits_sorted_descending(self):
        hits = [_hit(0.4), _hit(0.9), _hit(0.6), _hit(0.35)]
        ctx = {"smem_hits": hits}
        result = _rank_and_trim_context(ctx, pressure=0.80)
        scores = [h["score"] for h in result["smem_hits"]]
        assert scores == sorted(scores, reverse=True)

    def test_mem0_ctx_preserved_at_compress(self):
        ctx = {"mem0_ctx": "keep this", "smem_hits": []}
        result = _rank_and_trim_context(ctx, pressure=0.80)
        assert result["mem0_ctx"] == "keep this"

    def test_episodic_list_preserved_at_compress(self):
        ctx = {"episodic": [{"content": "old ep"}], "smem_hits": []}
        result = _rank_and_trim_context(ctx, pressure=0.80)
        assert result["episodic"] == [{"content": "old ep"}]

    def test_conversation_turns_all_kept_at_compress(self):
        ctx = {"conversation_turns": _turns(8), "smem_hits": []}
        result = _rank_and_trim_context(ctx, pressure=0.80)
        # At compress tier, turns are NOT trimmed
        assert len(result["conversation_turns"]) == 16  # 8 pairs × 2

    def test_exact_compress_threshold(self):
        ctx = {"smem_hits": [_hit(0.29), _hit(0.31)]}
        result = _rank_and_trim_context(ctx, pressure=_PRESSURE_COMPRESS)
        scores = [h["score"] for h in result["smem_hits"]]
        assert 0.29 not in scores
        assert 0.31 in scores

    def test_passthrough_keys_untouched(self):
        ctx = {
            "query": "what is 2+2",
            "system_prompt": "you are jarvis",
            "working_mem": "fact: user likes dark mode",
            "smem_hits": [],
        }
        result = _rank_and_trim_context(ctx, pressure=0.80)
        assert result["query"] == "what is 2+2"
        assert result["system_prompt"] == "you are jarvis"
        assert result["working_mem"] == "fact: user likes dark mode"


# ── 3. Switch tier (p ≥ 0.90) ─────────────────────────────────────────────────

class TestSwitchTier:
    def test_smem_below_0_5_dropped(self):
        hits = [_hit(0.3), _hit(0.49), _hit(0.5), _hit(0.8)]
        ctx = {"smem_hits": hits}
        result = _rank_and_trim_context(ctx, pressure=0.90)
        scores = [h["score"] for h in result["smem_hits"]]
        assert 0.3 not in scores
        assert 0.49 not in scores
        assert 0.5 in scores
        assert 0.8 in scores

    def test_smem_hits_sorted_descending_at_switch(self):
        hits = [_hit(0.5), _hit(0.95), _hit(0.7), _hit(0.6)]
        ctx = {"smem_hits": hits}
        result = _rank_and_trim_context(ctx, pressure=0.95)
        scores = [h["score"] for h in result["smem_hits"]]
        assert scores == sorted(scores, reverse=True)

    def test_mem0_ctx_cleared(self):
        ctx = {"mem0_ctx": "episodic data that should be gone", "smem_hits": []}
        result = _rank_and_trim_context(ctx, pressure=0.90)
        assert result["mem0_ctx"] == ""

    def test_episodic_list_cleared(self):
        ctx = {"episodic": [{"content": "old"}, {"content": "older"}], "smem_hits": []}
        result = _rank_and_trim_context(ctx, pressure=0.90)
        assert result["episodic"] == []

    def test_older_turns_dropped_recent_kept(self):
        # 6 pairs of turns = 12 messages; only last RECENT_TURNS_PROTECTED pairs kept
        ctx = {"conversation_turns": _turns(6), "smem_hits": []}
        result = _rank_and_trim_context(ctx, pressure=0.90)
        kept = result["conversation_turns"]
        # Protected window = RECENT_TURNS_PROTECTED pairs × 2 messages
        assert len(kept) == RECENT_TURNS_PROTECTED * 2

    def test_turns_exactly_at_protected_boundary(self):
        # Exactly RECENT_TURNS_PROTECTED pairs — nothing to drop
        ctx = {"conversation_turns": _turns(RECENT_TURNS_PROTECTED), "smem_hits": []}
        result = _rank_and_trim_context(ctx, pressure=0.90)
        assert len(result["conversation_turns"]) == RECENT_TURNS_PROTECTED * 2

    def test_fewer_turns_than_protected_all_kept(self):
        ctx = {"conversation_turns": _turns(1), "smem_hits": []}
        result = _rank_and_trim_context(ctx, pressure=0.95)
        assert len(result["conversation_turns"]) == 2  # 1 pair

    def test_exact_switch_threshold(self):
        ctx = {"mem0_ctx": "drop me", "smem_hits": [_hit(0.49), _hit(0.51)]}
        result = _rank_and_trim_context(ctx, pressure=_PRESSURE_SWITCH)
        assert result["mem0_ctx"] == ""
        scores = [h["score"] for h in result["smem_hits"]]
        assert 0.49 not in scores
        assert 0.51 in scores

    def test_all_smem_below_threshold_results_in_empty(self):
        hits = [_hit(0.1), _hit(0.2), _hit(0.3), _hit(0.4)]
        ctx = {"smem_hits": hits}
        result = _rank_and_trim_context(ctx, pressure=0.90)
        assert result["smem_hits"] == []


# ── 4. Edge cases ──────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_ctx_dict_no_error(self):
        result = _rank_and_trim_context({}, pressure=0.95)
        assert isinstance(result, dict)

    def test_no_smem_hits_key(self):
        ctx = {"mem0_ctx": "data", "conversation_turns": _turns(2)}
        result = _rank_and_trim_context(ctx, pressure=0.90)
        assert result["smem_hits"] == []

    def test_original_dict_not_mutated(self):
        hits = [_hit(0.1), _hit(0.9)]
        ctx = {"smem_hits": hits, "mem0_ctx": "keep", "episodic": [{"x": 1}]}
        original_len = len(ctx["smem_hits"])
        _rank_and_trim_context(ctx, pressure=0.95)
        # The original dict and its lists must be unchanged
        assert len(ctx["smem_hits"]) == original_len
        assert ctx["mem0_ctx"] == "keep"
        assert ctx["episodic"] == [{"x": 1}]


# ── 5. rank_context_blocks integration ────────────────────────────────────────

class TestRankContextBlocksPressureIntegration:
    def test_no_pressure_includes_low_score_hits(self):
        hits = [_hit(0.1, "low", "low relevance content"), _hit(0.9, "high", "high relevance content")]
        blocks = rank_context_blocks(smem_hits=hits, pressure=0.0)
        labels = [b["label"] for b in blocks]
        assert any("low" in lbl for lbl in labels)
        assert any("high" in lbl for lbl in labels)

    def test_compress_pressure_drops_low_smem_blocks(self):
        hits = [
            _hit(0.1, "very_low", "very low content"),
            _hit(0.8, "high", "high relevance content"),
        ]
        blocks = rank_context_blocks(smem_hits=hits, pressure=0.80)
        labels = [b["label"] for b in blocks]
        assert not any("very_low" in lbl for lbl in labels)
        assert any("high" in lbl for lbl in labels)

    def test_switch_pressure_clears_mem0_block(self):
        blocks = rank_context_blocks(
            smem_hits=[_hit(0.9, "good", "relevant content")],
            mem0_ctx="episodic memories",
            pressure=0.92,
        )
        labels = [b["label"] for b in blocks]
        assert "mem0" not in labels

    def test_switch_pressure_drops_borderline_smem(self):
        hits = [_hit(0.49, "borderline", "content"), _hit(0.51, "keeper", "content")]
        blocks = rank_context_blocks(smem_hits=hits, pressure=0.90)
        labels = [b["label"] for b in blocks]
        assert not any("borderline" in lbl for lbl in labels)
        assert any("keeper" in lbl for lbl in labels)

    def test_working_mem_always_present_under_pressure(self):
        blocks = rank_context_blocks(
            working_mem="important facts",
            smem_hits=[_hit(0.1, "x", "low content")],
            pressure=0.95,
        )
        labels = [b["label"] for b in blocks]
        assert "working_memory" in labels
