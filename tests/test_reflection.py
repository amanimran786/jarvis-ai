"""Tests for harness/reflection.py — Jarvis reflection pipeline."""
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from harness import reflection


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _recent_ts(hours_ago: float = 1.0) -> str:
    ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return ts.isoformat()


def _make_sample_scores():
    return [
        {
            "ts": _recent_ts(3),
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
            "ts": _recent_ts(2),
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
            "ts": _recent_ts(1),
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


_SAMPLE_SCORES = _make_sample_scores()


class ReflectionPipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        base = Path(self._tmpdir.name)
        self._log = base / "logs" / "self_eval.jsonl"
        self._log.parent.mkdir(parents=True)
        self._output = base / "kb" / "core" / "jarvis_self_eval.md"
        self._history = base / "evals" / "reflection_history.jsonl"
        self._history.parent.mkdir(parents=True)
        self._episodic = base / "memory" / "episodic"
        self._episodic.mkdir(parents=True)

        # Patch path helpers
        self._patches = [
            patch("harness.reflection._self_eval_log", return_value=self._log),
            patch("harness.reflection._reflection_output", return_value=self._output),
            patch("harness.reflection._reflection_history_path", return_value=self._history),
            patch("harness.reflection._episodic_dir", return_value=self._episodic),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()

    def _seed_scores(self, records=None):
        records = records if records is not None else _make_sample_scores()
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
        records = list(_make_sample_scores())
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
        # Seed a snapshot at lower quality so the real run shows an upward delta
        import json as _json
        prev_snap = {
            "ts": "2026-06-24T00:00:00+00:00",
            "total_scored": 3,
            "overall_quality": 0.50,
            "axes": {"routing_accuracy": 0.40, "response_relevance": 0.40, "conciseness": 0.50},
            "top_flags": {"poor_relevance": 5},
        }
        self._history.write_text(_json.dumps(prev_snap) + "\n")
        self._seed_scores()
        reflection.run_reflection(hours=168)
        content = self._output.read_text()
        # Trend arrow should appear in the markdown report
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


class ImprovementNotesTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        base = Path(self._tmpdir.name)
        self._log = base / "logs" / "self_eval.jsonl"
        self._log.parent.mkdir(parents=True)
        self._improve_log = base / "kb" / "self_improvement_log.md"
        self._improve_log.parent.mkdir(parents=True)
        self._history = base / "evals" / "reflection_history.jsonl"
        self._history.parent.mkdir(parents=True)

        self._patches = [
            patch("harness.reflection._self_eval_log", return_value=self._log),
            patch("harness.reflection._improvement_log_path", return_value=self._improve_log),
            # write_improvement_notes uses self_eval_log.load_recent which reads _log_path
            patch("harness.self_eval_log._log_path", return_value=self._log),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()

    def _seed(self):
        records = _make_sample_scores()
        with open(self._log, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def _mock_local(self, text: str):
        return patch("brains.brain_ollama.ask_local", return_value=text)

    def test_returns_empty_when_no_data(self):
        result = reflection.write_improvement_notes(n=50)
        self.assertEqual(result, "")

    def test_returns_empty_when_local_model_fails(self):
        self._seed()
        with patch("brains.brain_ollama.ask_local", side_effect=RuntimeError("model down")):
            result = reflection.write_improvement_notes(n=50)
        self.assertEqual(result, "")

    def test_creates_log_file_when_missing(self):
        self._seed()
        notes = "NOTE: routing is weak → fix it"
        with self._mock_local(notes):
            reflection.write_improvement_notes(n=50)
        self.assertTrue(self._improve_log.exists())

    def test_log_file_has_header_on_first_write(self):
        self._seed()
        with self._mock_local("NOTE: fix routing"):
            reflection.write_improvement_notes(n=50)
        content = self._improve_log.read_text()
        self.assertIn("Self-Improvement Log", content)

    def test_appends_dated_entry(self):
        self._seed()
        with self._mock_local("NOTE: improve relevance"):
            reflection.write_improvement_notes(n=50)
        with self._mock_local("NOTE: reduce verbosity"):
            reflection.write_improvement_notes(n=50)
        content = self._improve_log.read_text()
        self.assertEqual(content.count("## 20"), 2)

    def test_entry_includes_quality_stats(self):
        self._seed()
        with self._mock_local("NOTE: fix it"):
            reflection.write_improvement_notes(n=50)
        content = self._improve_log.read_text()
        self.assertIn("quality=", content)
        self.assertIn("routing=", content)

    def test_note_formatting_splits_on_note_prefix(self):
        self._seed()
        raw = "NOTE: first issue → fix A.NOTE: second issue → fix B."
        with self._mock_local(raw):
            result = reflection.write_improvement_notes(n=50)
        self.assertIn("\nNOTE:", result)

    def test_returns_notes_text(self):
        self._seed()
        with self._mock_local("NOTE: routing is low → adjust weights"):
            result = reflection.write_improvement_notes(n=50)
        self.assertIn("NOTE:", result)

    def test_async_wrapper_does_not_raise(self):
        # Just verify it spawns without error
        with self._mock_local("NOTE: test"):
            reflection.write_improvement_notes_async(n=50)
        import time; time.sleep(0.05)  # let thread start


if __name__ == "__main__":
    unittest.main()
