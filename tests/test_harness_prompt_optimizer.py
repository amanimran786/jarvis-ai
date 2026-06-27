"""Tests for harness/prompt_optimizer.py — self-eval driven prompt suggestions."""
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from harness import prompt_optimizer


def _recent_ts(hours_ago: float = 1.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _make_records(n: int = 10, *, quality: float = 0.55, route: str = "Unknown",
                  flags: list | None = None, routing: float = 0.50,
                  relevance: float = 0.45) -> list[dict]:
    return [
        {
            "ts": _recent_ts(n - i),
            "query": f"test query {i}",
            "route": route,
            "routing_accuracy": routing,
            "response_relevance": relevance,
            "conciseness": 0.80,
            "response_quality": quality,
            "flags": flags if flags is not None else ["poor_relevance"],
        }
        for i in range(n)
    ]


def _patch_load(records):
    return patch("harness.prompt_optimizer._load_records", return_value=records)


def _patch_avg(avg_dict):
    return patch("harness.self_eval_log.rolling_average", return_value=avg_dict)


def _patch_local(text: str):
    return patch("brains.brain_ollama.ask_local", return_value=text)


_GOOD_LLM_OUTPUT = """\
SUGGESTION_START
id: s001
pattern: flag:poor_relevance
target: identity.md §Routing Rules
rationale: Adding explicit routing keywords reduces relevance mismatches.
confidence: 0.75
current_text: |
  Route queries by domain.
suggested_text: |
  Route queries by domain. For research queries, always invoke the browser tool.
SUGGESTION_END
SUGGESTION_START
id: s002
pattern: route:Unknown
target: identity.md §Behavior
rationale: Unrouted queries need a fallback specificity rule.
confidence: 0.55
current_text: |
  (none — new addition)
suggested_text: |
  When the route is unknown, ask one clarifying question before responding.
SUGGESTION_END"""


class FindLowPatternsTests(unittest.TestCase):
    def test_empty_records_returns_empty(self):
        result = prompt_optimizer._find_low_patterns([])
        self.assertEqual(result, [])

    def test_low_quality_route_surfaces_bucket(self):
        records = _make_records(5, quality=0.55, route="Calendar")
        buckets = prompt_optimizer._find_low_patterns(records)
        keys = [b.key for b in buckets]
        self.assertIn("route:Calendar", keys)

    def test_high_quality_route_not_surfaced(self):
        records = _make_records(5, quality=0.90, route="TechAssist", flags=[],
                                routing=0.90, relevance=0.90)
        buckets = prompt_optimizer._find_low_patterns(records)
        keys = [b.key for b in buckets]
        self.assertNotIn("route:TechAssist", keys)

    def test_flag_bucket_surfaced_when_low(self):
        records = _make_records(5, quality=0.50, flags=["poor_relevance"])
        buckets = prompt_optimizer._find_low_patterns(records)
        keys = [b.key for b in buckets]
        self.assertIn("flag:poor_relevance", keys)

    def test_bucket_below_min_samples_ignored(self):
        records = _make_records(2, quality=0.30, route="Rare")
        buckets = prompt_optimizer._find_low_patterns(records)
        keys = [b.key for b in buckets]
        self.assertNotIn("route:Rare", keys)

    def test_buckets_sorted_worst_first(self):
        records = (
            _make_records(5, quality=0.40, route="Bad") +
            _make_records(5, quality=0.58, route="Medium", flags=[])
        )
        buckets = prompt_optimizer._find_low_patterns(records)
        qualities = [b.avg_quality for b in buckets if b.avg_quality is not None]
        self.assertEqual(qualities, sorted(qualities))

    def test_capped_at_six_patterns(self):
        records = []
        for i in range(10):
            records += _make_records(4, quality=0.40, route=f"Route{i}", flags=[])
        buckets = prompt_optimizer._find_low_patterns(records)
        self.assertLessEqual(len(buckets), 6)


class ParseSuggestionsTests(unittest.TestCase):
    def test_parses_two_suggestions(self):
        suggestions = prompt_optimizer._parse_suggestions(_GOOD_LLM_OUTPUT)
        self.assertEqual(len(suggestions), 2)

    def test_suggestion_fields_extracted(self):
        suggestions = prompt_optimizer._parse_suggestions(_GOOD_LLM_OUTPUT)
        s = suggestions[0]
        self.assertIn("poor_relevance", s.pattern)
        self.assertIn("identity.md", s.target)
        self.assertGreater(s.confidence, 0.0)
        self.assertIn("browser tool", s.suggested_text)

    def test_empty_input_returns_empty(self):
        result = prompt_optimizer._parse_suggestions("")
        self.assertEqual(result, [])

    def test_malformed_block_skipped_gracefully(self):
        raw = "SUGGESTION_START\nbroken content\nSUGGESTION_END"
        result = prompt_optimizer._parse_suggestions(raw)
        self.assertIsInstance(result, list)

    def test_confidence_defaults_to_half_on_bad_value(self):
        raw = _GOOD_LLM_OUTPUT.replace("confidence: 0.75", "confidence: not-a-number")
        suggestions = prompt_optimizer._parse_suggestions(raw)
        self.assertEqual(suggestions[0].confidence, 0.5)


class RunOptimizerTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        base = Path(self._tmpdir.name)
        self._suggestions_file = base / "kb" / "prompt_suggestions.md"
        self._suggestions_file.parent.mkdir(parents=True)
        self._identity_file = base / "kb" / "core" / "identity.md"
        self._identity_file.parent.mkdir(parents=True)
        self._identity_file.write_text("# Identity\n\nRoute queries by domain.\n")

        self._patches = [
            patch("harness.prompt_optimizer._suggestions_path",
                  return_value=self._suggestions_file),
            patch("harness.prompt_optimizer._identity_path",
                  return_value=self._identity_file),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()

    def _avg(self, q=0.55):
        return {"count": 10, "response_quality": q, "routing_accuracy": 0.50,
                "response_relevance": 0.45, "conciseness": 0.80, "top_flags": {}}

    def test_no_records_returns_error_result(self):
        with _patch_load([]):
            result = prompt_optimizer.run_optimizer(n=200)
        self.assertEqual(result.n_analyzed, 0)
        self.assertIn("No scored records", result.error)

    def test_writes_suggestions_file(self):
        records = _make_records(10)
        with _patch_load(records), _patch_avg(self._avg()), _patch_local(_GOOD_LLM_OUTPUT):
            prompt_optimizer.run_optimizer(n=200)
        self.assertTrue(self._suggestions_file.exists())

    def test_suggestions_file_has_required_header(self):
        records = _make_records(10)
        with _patch_load(records), _patch_avg(self._avg()), _patch_local(_GOOD_LLM_OUTPUT):
            prompt_optimizer.run_optimizer(n=200)
        content = self._suggestions_file.read_text()
        self.assertIn("Prompt Suggestions", content)
        self.assertIn("Low-Scoring Patterns", content)

    def test_llm_failure_still_writes_file(self):
        records = _make_records(10)
        with _patch_load(records), _patch_avg(self._avg()), \
             patch("brains.brain_ollama.ask_local", side_effect=RuntimeError("down")):
            result = prompt_optimizer.run_optimizer(n=200)
        self.assertTrue(self._suggestions_file.exists())
        self.assertIn("failed", result.error.lower())

    def test_n_analyzed_matches_record_count(self):
        records = _make_records(15)
        with _patch_load(records), _patch_avg(self._avg()), _patch_local(""):
            result = prompt_optimizer.run_optimizer(n=200)
        self.assertEqual(result.n_analyzed, 15)

    def test_no_patterns_still_writes_file(self):
        records = _make_records(10, quality=0.95, route="Perfect", flags=[],
                                routing=0.95, relevance=0.95)
        with _patch_load(records), _patch_avg(self._avg(q=0.95)), _patch_local(""):
            result = prompt_optimizer.run_optimizer(n=200)
        self.assertTrue(self._suggestions_file.exists())
        self.assertEqual(result.low_score_patterns, [])


class ApplySuggestionTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        base = Path(self._tmpdir.name)
        self._suggestions_file = base / "kb" / "prompt_suggestions.md"
        self._suggestions_file.parent.mkdir(parents=True)
        self._identity_file = base / "kb" / "core" / "identity.md"
        self._identity_file.parent.mkdir(parents=True)

        self._patches = [
            patch("harness.prompt_optimizer._suggestions_path",
                  return_value=self._suggestions_file),
            patch("harness.prompt_optimizer._identity_path",
                  return_value=self._identity_file),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()

    def _write_suggestions(self, current: str, suggested: str, sid: str = "s001",
                           target: str = "identity.md §Routing"):
        content = (
            f"# Jarvis Prompt Suggestions\n\n"
            f"### [{sid}] {target}  *(confidence: 75% — high)*\n"
            f"**Pattern addressed:** flag:poor_relevance\n"
            f"**Rationale:** Fixes routing.\n\n"
            f"**Current:**\n```\n{current}\n```\n\n"
            f"**Suggested:**\n```\n{suggested}\n```\n\n"
            f"*Apply: `/optimize apply {sid}`*\n"
        )
        self._suggestions_file.write_text(content)

    def test_no_suggestions_file_returns_error(self):
        result = prompt_optimizer.apply_suggestion("s001")
        self.assertIn("No suggestions file", result)

    def test_unknown_id_returns_error(self):
        self._suggestions_file.write_text("# Jarvis Prompt Suggestions\n")
        result = prompt_optimizer.apply_suggestion("s999")
        self.assertIn("not found", result)

    def test_applies_to_identity_md(self):
        original = "When uncertain, ask a clarifying question."
        replacement = "Always use explicit routing tags before responding."
        self._identity_file.write_text(f"# Identity\n\n{original}\n")
        self._write_suggestions(original, replacement)
        prompt_optimizer.apply_suggestion("s001")
        new_content = self._identity_file.read_text()
        self.assertIn(replacement, new_content)
        self.assertNotIn(original, new_content)

    def test_shows_diff_in_reply(self):
        original = "When uncertain, ask a clarifying question."
        replacement = "Always use explicit routing tags before responding."
        self._identity_file.write_text(f"# Identity\n\n{original}\n")
        self._write_suggestions(original, replacement)
        result = prompt_optimizer.apply_suggestion("s001")
        self.assertIn("---", result)
        self.assertIn("+++", result)

    def test_non_identity_target_does_not_write_file(self):
        self._identity_file.write_text("# Identity\n\nsome text\n")
        self._write_suggestions("old", "new", target="system prompt §Behavior")
        result = prompt_optimizer.apply_suggestion("s001")
        self.assertIn("manual", result.lower())
        self.assertIn("some text", self._identity_file.read_text())

    def test_current_text_not_found_returns_error(self):
        self._identity_file.write_text("# Identity\n\ncompletely different content\n")
        self._write_suggestions("text that is not in file", "new text")
        result = prompt_optimizer.apply_suggestion("s001")
        self.assertIn("Could not find", result)

    def test_new_addition_appended(self):
        self._identity_file.write_text("# Identity\n\nexisting content\n")
        self._write_suggestions("(none — new addition)", "## New Section\nAdded content.")
        result = prompt_optimizer.apply_suggestion("s001")
        new_content = self._identity_file.read_text()
        self.assertIn("New Section", new_content)
        self.assertIn("appended", result)


class OptimizeTextTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        base = Path(self._tmpdir.name)
        self._suggestions_file = base / "kb" / "prompt_suggestions.md"
        self._suggestions_file.parent.mkdir(parents=True)
        self._identity_file = base / "kb" / "core" / "identity.md"
        self._identity_file.parent.mkdir(parents=True)
        self._identity_file.write_text("# Identity\n")
        self._patches = [
            patch("harness.prompt_optimizer._suggestions_path",
                  return_value=self._suggestions_file),
            patch("harness.prompt_optimizer._identity_path",
                  return_value=self._identity_file),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()

    def _avg(self):
        return {"count": 10, "response_quality": 0.55, "routing_accuracy": 0.50,
                "response_relevance": 0.45, "conciseness": 0.80, "top_flags": {}}

    def test_no_records_returns_no_data_message(self):
        with _patch_load([]):
            result = prompt_optimizer.optimize_text()
        self.assertIn("No self-eval data", result)

    def test_returns_analyzed_count(self):
        records = _make_records(10)
        with _patch_load(records), _patch_avg(self._avg()), _patch_local(_GOOD_LLM_OUTPUT):
            result = prompt_optimizer.optimize_text()
        self.assertIn("analyzed", result)
        self.assertIn("10", result)

    def test_mentions_apply_command(self):
        records = _make_records(10)
        with _patch_load(records), _patch_avg(self._avg()), _patch_local(_GOOD_LLM_OUTPUT):
            result = prompt_optimizer.optimize_text()
        self.assertIn("apply", result.lower())

    def test_mentions_output_path(self):
        records = _make_records(10)
        with _patch_load(records), _patch_avg(self._avg()), _patch_local(_GOOD_LLM_OUTPUT):
            result = prompt_optimizer.optimize_text()
        self.assertIn("prompt_suggestions.md", result)


if __name__ == "__main__":
    unittest.main()
