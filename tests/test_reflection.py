"""Tests for harness/reflection.py — Jarvis reflection pipeline."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import reflection


# ── Fixtures ──────────────────────────────────────────────────────────────────

_SAMPLE_SCORES = [
    {
        "ts": "2026-06-25T00:00:00+00:00",
        "query": "debug my python error",
        "route": "TechAssist",
        "routing_accuracy": 0.9,
        "response_relevance": 0.8,
        "conciseness": 1.0,
        "response_quality": 0.87,
        "flags": [],
        "reflection_note": "Good quality overall",
    },
    {
        "ts": "2026-06-25T01:00:00+00:00",
        "query": "what's on my calendar",
        "route": "Calendar",
        "routing_accuracy": 0.9,
        "response_relevance": 0.55,
        "conciseness": 0.9,
        "response_quality": 0.73,
        "flags": ["poor_relevance"],
        "reflection_note": "Response didn't address the query well",
    },
    {
        "ts": "2026-06-25T02:00:00+00:00",
        "query": "interview prep behavioral",
        "route": "InterviewIntel",
        "routing_accuracy": 0.9,
        "response_relevance": 0.72,
        "conciseness": 0.5,
        "response_quality": 0.68,
        "flags": ["verbose"],
        "reflection_note": "Response was verbose",
    },
]


class ReflectionPipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        base = Path(self._tmpdir.name)
        self._log = base / "logs" / "self_eval.jsonl"
        self._log.parent.mkdir(parents=True)
        self._output = base / "kb" / "core" / "jarvis_self_eval.md"
        self._episodic = base / "memory" / "episodic"
        self._episodic.mkdir(parents=True)

        # Patch path helpers
        self._patches = [
            patch("harness.reflection._self_eval_log", return_value=self._log),
            patch("harness.reflection._reflection_output", return_value=self._output),
            patch("harness.reflection._episodic_dir", return_value=self._episodic),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()

    def _seed_scores(self, records=None):
        records = records or _SAMPLE_SCORES
        with open(self._log, "w") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

    def test_no_scores_returns_zero_count_result(self):
        result = reflection.run_reflection(hours=168)
        self.assertEqual(result.total_scored, 0)
        self.assertIsNone(result.overall_quality)

    def test_reflection_writes_markdown_file(self):
        self._seed_scores()
        reflection.run_reflection(hours=168)
        self.assertTrue(self._output.exists())
        content = self._output.read_text()
        self.assertIn("# Jarvis Self-Eval Reflection", content)

    def test_overall_quality_computed(self):
        self._seed_scores()
        result = reflection.run_reflection(hours=168)
        self.assertEqual(result.total_scored, 3)
        self.assertIsNotNone(result.overall_quality)
        self.assertGreater(result.overall_quality, 0.0)
        self.assertLessEqual(result.overall_quality, 1.0)

    def test_axis_averages_present(self):
        self._seed_scores()
        result = reflection.run_reflection(hours=168)
        for axis in ("routing_accuracy", "response_relevance", "conciseness"):
            self.assertIn(axis, result.axis_averages)
            val = result.axis_averages[axis]
            self.assertIsNotNone(val)
            self.assertGreaterEqual(val, 0.0)
            self.assertLessEqual(val, 1.0)

    def test_top_flags_counted(self):
        self._seed_scores()
        result = reflection.run_reflection(hours=168)
        # "poor_relevance" and "verbose" are in the sample
        self.assertIn("poor_relevance", result.top_flags)
        self.assertIn("verbose", result.top_flags)
        self.assertEqual(result.top_flags["poor_relevance"], 1)
        self.assertEqual(result.top_flags["verbose"], 1)

    def test_domain_stats_grouped_by_route(self):
        self._seed_scores()
        result = reflection.run_reflection(hours=168)
        self.assertIn("TechAssist", result.domain_stats)
        self.assertIn("Calendar", result.domain_stats)
        self.assertIn("InterviewIntel", result.domain_stats)
        self.assertEqual(result.domain_stats["TechAssist"].count, 1)

    def test_insights_non_empty(self):
        self._seed_scores()
        result = reflection.run_reflection(hours=168)
        self.assertGreater(len(result.insights), 0)
        for insight in result.insights:
            self.assertIsInstance(insight, str)
            self.assertGreater(len(insight), 10)

    def test_markdown_has_required_sections(self):
        self._seed_scores()
        reflection.run_reflection(hours=168)
        content = self._output.read_text()
        for section in ("Performance Summary", "Axis Breakdown", "Insights", "Action Items"):
            self.assertIn(section, content, msg=f"Missing section: {section}")

    def test_lookback_window_filters_old_records(self):
        # Mix old and new records
        records = list(_SAMPLE_SCORES)
        records.append({
            "ts": "2020-01-01T00:00:00+00:00",  # way in the past
            "query": "old query",
            "route": "Calendar",
            "routing_accuracy": 0.5,
            "response_relevance": 0.5,
            "conciseness": 0.5,
            "response_quality": 0.5,
            "flags": [],
            "reflection_note": "Old record",
        })
        self._seed_scores(records)
        result = reflection.run_reflection(hours=24)  # only last 24h
        # The 3 sample records are within 24h; the old one is filtered
        self.assertEqual(result.total_scored, 3)

    def test_error_records_skipped(self):
        with open(self._log, "w") as f:
            f.write(json.dumps({"ts": "2026-06-25T00:00:00+00:00", "error": True}) + "\n")
            for rec in _SAMPLE_SCORES:
                f.write(json.dumps(rec) + "\n")
        result = reflection.run_reflection(hours=168)
        self.assertEqual(result.total_scored, 3)  # error record excluded

    def test_episodic_context_loaded_from_json(self):
        # Plant an episodic memory file
        ep_file = self._episodic / "test_episode.json"
        ep_file.write_text(json.dumps({
            "domain": "professional",
            "content": "Active YouTube PM interview prep as of June 2026.",
        }))
        result = reflection.run_reflection(hours=168)
        # May or may not have records but episodic is loaded
        self.assertIsInstance(result.episodic_context, list)

    def test_reflect_text_returns_string_summary(self):
        self._seed_scores()
        text = reflection.reflect_text(hours=168)
        self.assertIsInstance(text, str)
        self.assertIn("Reflection complete", text)
        self.assertIn("responses scored", text)

    def test_reflect_text_empty_when_no_data(self):
        text = reflection.reflect_text(hours=168)
        self.assertIn("no responses scored", text.lower())

    def test_trend_shown_when_previous_output_exists(self):
        # Write a previous reflection with a known quality
        self._output.parent.mkdir(parents=True, exist_ok=True)
        self._output.write_text("Overall quality: 0.70/1.0\n")
        self._seed_scores()
        reflection.run_reflection(hours=168)
        content = self._output.read_text()
        # Trend arrow should appear
        self.assertTrue("↑" in content or "↓" in content)

    def test_never_raises_on_malformed_log(self):
        self._log.write_text("not json\n{broken\n")
        result = reflection.run_reflection(hours=168)
        self.assertIsInstance(result, reflection.ReflectionResult)


class DomainStatsTests(unittest.TestCase):
    def test_avg_returns_none_when_no_data(self):
        ds = reflection.DomainStats(route="Test")
        self.assertIsNone(ds.avg("routing_accuracy"))

    def test_quality_zero_when_no_responses(self):
        ds = reflection.DomainStats(route="Test")
        self.assertEqual(ds.quality(), 0.0)

    def test_avg_computed_correctly(self):
        ds = reflection.DomainStats(route="Test")
        ds.count = 2
        ds.routing_accuracy_sum = 1.6
        self.assertEqual(ds.avg("routing_accuracy"), 0.8)


if __name__ == "__main__":
    unittest.main()
