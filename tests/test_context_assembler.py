"""Unit tests for context_assembler — priority ranking and pressure trimming."""
import unittest

import context_assembler as asm


class ScoreToPriorityTests(unittest.TestCase):
    def test_low_score_maps_to_low_priority(self):
        self.assertEqual(asm._score_to_priority(0.3), 65)

    def test_high_score_maps_to_high_priority(self):
        self.assertEqual(asm._score_to_priority(1.0), 95)

    def test_midpoint_score_maps_to_midpoint_priority(self):
        result = asm._score_to_priority(0.65)
        self.assertAlmostEqual(result, 80, delta=1)

    def test_below_floor_clamped(self):
        self.assertEqual(asm._score_to_priority(0.0), 65)

    def test_above_ceil_clamped(self):
        self.assertEqual(asm._score_to_priority(2.0), 95)


class RankContextBlocksTests(unittest.TestCase):
    def test_empty_inputs_produce_empty_list(self):
        blocks = asm.rank_context_blocks()
        self.assertEqual(blocks, [])

    def test_working_mem_gets_priority_98(self):
        blocks = asm.rank_context_blocks(working_mem="hello")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["label"], "working_memory")
        self.assertEqual(blocks[0]["priority"], 98)

    def test_smem_hit_gets_per_score_priority(self):
        hits = [{"content": "fact A", "score": 0.8, "id": "a"}]
        blocks = asm.rank_context_blocks(smem_hits=hits)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["label"], "semantic:a")
        expected_priority = asm._score_to_priority(0.8)
        self.assertEqual(blocks[0]["priority"], expected_priority)

    def test_multiple_smem_hits_produce_individual_blocks(self):
        hits = [
            {"content": "fact A", "score": 0.9, "id": "a"},
            {"content": "fact B", "score": 0.4, "id": "b"},
        ]
        blocks = asm.rank_context_blocks(smem_hits=hits)
        labels = [b["label"] for b in blocks]
        self.assertIn("semantic:a", labels)
        self.assertIn("semantic:b", labels)
        # higher score → higher priority
        pa = next(b["priority"] for b in blocks if b["label"] == "semantic:a")
        pb = next(b["priority"] for b in blocks if b["label"] == "semantic:b")
        self.assertGreater(pa, pb)

    def test_smem_hit_without_content_is_skipped(self):
        hits = [{"content": "", "score": 0.9, "id": "empty"}]
        blocks = asm.rank_context_blocks(smem_hits=hits)
        self.assertEqual(blocks, [])

    def test_full_priority_ordering(self):
        hits = [{"content": "smem fact", "score": 0.5, "id": "s"}]
        blocks = asm.rank_context_blocks(
            working_mem="wm",
            repeat_ctx="rc",
            vault_ctx="vc",
            graph_ctx="gc",
            semantic_hint="sh",
            smem_hits=hits,
            mem0_ctx="m0",
        )
        labels = [b["label"] for b in blocks]
        self.assertEqual(labels, ["working_memory", "repeat_context", "vault", "graph", "semantic_hint", "semantic:s", "mem0"])
        # Priorities are not globally sorted — compile_context_blocks handles that.
        # Verify each label carries the expected priority.
        by_label = {b["label"]: b["priority"] for b in blocks}
        self.assertEqual(by_label["working_memory"], 98)
        self.assertEqual(by_label["repeat_context"], 96)
        self.assertEqual(by_label["vault"], 90)
        self.assertEqual(by_label["graph"], 75)
        self.assertEqual(by_label["semantic_hint"], 70)
        self.assertEqual(by_label["semantic:s"], asm._score_to_priority(0.5))
        self.assertEqual(by_label["mem0"], 55)

    def test_compress_pressure_drops_low_score_smem(self):
        hits = [
            {"content": "high", "score": 0.8, "id": "h"},
            {"content": "low", "score": 0.2, "id": "l"},
        ]
        blocks = asm.rank_context_blocks(smem_hits=hits, pressure=0.80)
        labels = [b["label"] for b in blocks]
        self.assertIn("semantic:h", labels)
        self.assertNotIn("semantic:l", labels)

    def test_switch_pressure_drops_mem0_and_strict_smem(self):
        hits = [
            {"content": "high", "score": 0.8, "id": "h"},
            {"content": "mid", "score": 0.4, "id": "m"},
        ]
        blocks = asm.rank_context_blocks(
            smem_hits=hits,
            mem0_ctx="episodic data",
            pressure=0.92,
        )
        labels = [b["label"] for b in blocks]
        self.assertIn("semantic:h", labels)
        self.assertNotIn("semantic:m", labels)
        self.assertNotIn("mem0", labels)

    def test_below_threshold_pressure_passes_all_through(self):
        hits = [{"content": "low", "score": 0.2, "id": "l"}]
        blocks = asm.rank_context_blocks(smem_hits=hits, mem0_ctx="m0", pressure=0.5)
        labels = [b["label"] for b in blocks]
        self.assertIn("semantic:l", labels)
        self.assertIn("mem0", labels)


class RankConversationMessagesTests(unittest.TestCase):
    def _make_messages(self, n):
        msgs = []
        for i in range(n):
            msgs.append({"role": "user", "content": f"user turn {i}"})
            msgs.append({"role": "assistant", "content": f"assistant turn {i}"})
        return msgs

    def test_empty_returns_empty(self):
        self.assertEqual(asm.rank_conversation_messages([]), [])

    def test_short_list_returned_unchanged_order(self):
        msgs = self._make_messages(2)  # 4 messages, under protection window
        result = asm.rank_conversation_messages(msgs, recent_protected=3)
        self.assertEqual(result, msgs)

    def test_recent_turns_placed_last(self):
        msgs = self._make_messages(5)  # 10 messages
        result = asm.rank_conversation_messages(msgs, recent_protected=3)
        # Last 6 messages should be the 3 most recent pairs
        self.assertEqual(result[-6:], msgs[-6:])
        # First 4 should be the older ones
        self.assertEqual(result[:4], msgs[:4])

    def test_order_preserved_within_older_and_recent(self):
        msgs = self._make_messages(4)  # 8 messages, protected=3 → 6 recent, 2 older
        result = asm.rank_conversation_messages(msgs, recent_protected=3)
        older = result[:-6]
        recent = result[-6:]
        self.assertEqual(older, msgs[:2])
        self.assertEqual(recent, msgs[2:])


class RankAndTrimContextTests(unittest.TestCase):
    def test_no_op_below_threshold(self):
        ctx = {"smem_hits": [{"content": "x", "score": 0.1}], "mem0_ctx": "mem"}
        result = asm._rank_and_trim_context(ctx, 0.5)
        self.assertIs(result, ctx)  # same object returned unchanged

    def test_compress_tier_filters_smem(self):
        ctx = {
            "smem_hits": [
                {"content": "ok", "score": 0.5},
                {"content": "weak", "score": 0.1},
            ]
        }
        result = asm._rank_and_trim_context(ctx, 0.80)
        scores = [h["score"] for h in result["smem_hits"]]
        self.assertNotIn(0.1, scores)
        self.assertIn(0.5, scores)

    def test_switch_tier_clears_episodic_and_turns(self):
        msgs = [{"role": "user", "content": f"u{i}"} for i in range(10)]
        ctx = {
            "smem_hits": [{"content": "high", "score": 0.9}],
            "mem0_ctx": "episodic data",
            "episodic": [{"content": "old"}],
            "conversation_turns": msgs,
        }
        result = asm._rank_and_trim_context(ctx, 0.91)
        self.assertEqual(result["mem0_ctx"], "")
        self.assertEqual(result["episodic"], [])
        self.assertLessEqual(len(result["conversation_turns"]), asm.RECENT_TURNS_PROTECTED * 2)

    def test_does_not_mutate_caller_dict(self):
        original_hits = [{"content": "x", "score": 0.1}]
        ctx = {"smem_hits": original_hits}
        asm._rank_and_trim_context(ctx, 0.80)
        self.assertIs(ctx["smem_hits"], original_hits)


if __name__ == "__main__":
    unittest.main()
