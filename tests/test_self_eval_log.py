"""Tests for harness/self_eval_log.py — 3-axis response quality scorer."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import self_eval_log


# ── Fixture responses ─────────────────────────────────────────────────────────

_SPECIFIC_RESPONSE = (
    "Your data quality project is the strongest anchor for the YouTube PM role. "
    "Lead with the 40% metadata accuracy improvement and connect it to creator "
    "monetization — that's the L5 signal interviewers look for at YouTube. "
    "Keep it to 90 seconds: situation, your decision to prioritize, the metric, the outcome."
)

_FILLER_RESPONSE = (
    "Absolutely! Certainly, I would be happy to help. In conclusion, to prepare "
    "for an interview you should practice. It is important to note that preparation "
    "is key. I hope this helps! Please don't hesitate to ask for more."
)

_SHORT_ERROR_RESPONSE = "Local model error — model unavailable."


# ── Axis 1: routing_accuracy ──────────────────────────────────────────────────

class RoutingAccuracyTests(unittest.TestCase):
    def test_aligned_calendar_query(self):
        score = self_eval_log._score_routing_accuracy("what's on my calendar today", "Calendar")
        self.assertGreaterEqual(score, 0.80)

    def test_aligned_interview_query(self):
        score = self_eval_log._score_routing_accuracy("mock interview question for youtube pm", "InterviewIntel")
        self.assertGreaterEqual(score, 0.80)

    def test_misaligned_code_query_goes_to_daily(self):
        score = self_eval_log._score_routing_accuracy("debug this python error", "DailyOS")
        self.assertLess(score, 0.60)

    def test_empty_route_returns_neutral(self):
        score = self_eval_log._score_routing_accuracy("what's up", "")
        self.assertEqual(score, 0.5)

    def test_no_domain_signal_returns_mid_high(self):
        # Generic chit-chat — no routing failure expected
        score = self_eval_log._score_routing_accuracy("hello", "General")
        self.assertGreaterEqual(score, 0.70)

    def test_scores_clamped_to_unit_interval(self):
        score = self_eval_log._score_routing_accuracy("interview prep behavioral star", "InterviewIntel")
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)


# ── Axis 2: response_relevance ────────────────────────────────────────────────

class ResponseRelevanceTests(unittest.TestCase):
    def test_empty_response_scores_zero(self):
        self.assertEqual(self_eval_log._score_response_relevance("any query", ""), 0.0)

    def test_error_marker_scores_low(self):
        score = self_eval_log._score_response_relevance("debug my code", _SHORT_ERROR_RESPONSE)
        self.assertLess(score, 0.30)

    def test_filler_heavy_response_penalized(self):
        # Filler response still contains "interview"/"prepare" (overlap), so relevance
        # isn't zero — just capped by the 0.25 filler penalty. Score ~0.65, below 0.75.
        score = self_eval_log._score_response_relevance("interview prep", _FILLER_RESPONSE)
        self.assertLess(score, 0.75)

    def test_specific_relevant_response_scores_high(self):
        score = self_eval_log._score_response_relevance(
            "prepare for youtube pm interview", _SPECIFIC_RESPONSE
        )
        self.assertGreater(score, 0.55)

    def test_very_short_response_to_substantive_query(self):
        score = self_eval_log._score_response_relevance(
            "explain the difference between sql joins",
            "It varies."
        )
        self.assertLess(score, 0.50)

    def test_scores_clamped_to_unit_interval(self):
        score = self_eval_log._score_response_relevance("test query", _SPECIFIC_RESPONSE)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)


# ── Axis 3: conciseness ───────────────────────────────────────────────────────

class ConcisenessTests(unittest.TestCase):
    def test_empty_response_returns_midpoint(self):
        self.assertEqual(self_eval_log._score_conciseness("query", ""), 0.5)

    def test_short_conversational_reply_scores_perfect(self):
        score = self_eval_log._score_conciseness(
            "what time is it",
            "It's 3pm Pacific."
        )
        self.assertEqual(score, 1.0)

    def test_interview_response_within_range_scores_perfect(self):
        # ~40 words — within 150-300 word range? No, too short — penalty expected
        score = self_eval_log._score_conciseness(
            "tell me about your data project for a mock interview",
            _SPECIFIC_RESPONSE
        )
        # Should be penalized for being too short for an interview response
        self.assertLess(score, 1.0)

    def test_excessively_verbose_response_penalized(self):
        # 200-word response to a simple calendar query (expects ≤80 words)
        long_response = " ".join(["word"] * 200)
        score = self_eval_log._score_conciseness("what's on my calendar", long_response)
        self.assertLess(score, 0.60)

    def test_scores_are_rounded_to_3_decimals(self):
        score = self_eval_log._score_conciseness(
            "should i take this job offer", "Yes, take it. The comp is competitive and the team has strong growth trajectory."
        )
        # No more than 3 decimal places
        self.assertEqual(score, round(score, 3))

    def test_scores_clamped_to_unit_interval(self):
        score = self_eval_log._score_conciseness("test", " ".join(["x"] * 500))
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)


# ── Flag detection ────────────────────────────────────────────────────────────

class FlagDetectionTests(unittest.TestCase):
    def _flags(self, q, r, route="", ra=0.8, rr=0.8, cs=0.8):
        return self_eval_log._detect_flags(q, r, route, ra, rr, cs)

    def test_no_content_for_empty_response(self):
        flags = self._flags("query", "")
        self.assertIn("no_content", flags)

    def test_error_response_flag(self):
        flags = self._flags("query", "Local model error: timeout", ra=0.8, rr=0.8, cs=0.8)
        self.assertIn("error_response", flags)

    def test_routing_mismatch_flag_when_accuracy_low(self):
        flags = self._flags("query", "some response", ra=0.35, rr=0.8, cs=0.8)
        self.assertIn("routing_mismatch", flags)

    def test_verbose_flag_when_conciseness_low(self):
        flags = self._flags("query", "some response", ra=0.8, rr=0.8, cs=0.40)
        self.assertIn("verbose", flags)

    def test_filler_heavy_flag_for_multiple_fillers(self):
        flags = self._flags("query", _FILLER_RESPONSE, ra=0.8, rr=0.8, cs=0.8)
        self.assertIn("filler_heavy", flags)

    def test_clean_specific_response_has_no_flags(self):
        flags = self._flags("interview prep", _SPECIFIC_RESPONSE, route="InterviewIntel",
                            ra=0.9, rr=0.8, cs=0.9)
        self.assertEqual(flags, [])


# ── Composite score ───────────────────────────────────────────────────────────

class CompositeScoreTests(unittest.TestCase):
    def test_all_perfect_gives_one(self):
        self.assertEqual(self_eval_log._composite(1.0, 1.0, 1.0), 1.0)

    def test_all_zero_gives_zero(self):
        self.assertEqual(self_eval_log._composite(0.0, 0.0, 0.0), 0.0)

    def test_composite_never_exceeds_one(self):
        result = self_eval_log._composite(1.0, 1.0, 1.0)
        self.assertLessEqual(result, 1.0)

    def test_relevance_weighted_highest(self):
        # response_relevance weight is 0.40 — highest weight
        score_low_rel = self_eval_log._composite(1.0, 0.0, 1.0)  # low relevance
        score_low_route = self_eval_log._composite(0.0, 1.0, 1.0)  # low routing
        self.assertLess(score_low_rel, score_low_route)


# ── Integration: score() ──────────────────────────────────────────────────────

class ScoreFunctionTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._log = Path(self._tmpdir.name) / "self_eval.jsonl"
        self._patcher = patch(
            "harness.self_eval_log._log_path",
            return_value=self._log,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_score_writes_to_log(self):
        self.assertFalse(self._log.exists())
        self_eval_log.score("what's on my calendar", "You have a 2pm meeting.", "Calendar")
        self.assertTrue(self._log.exists())

    def test_score_record_has_required_fields(self):
        rec = self_eval_log.score("interview prep", _SPECIFIC_RESPONSE, "InterviewIntel")
        for field in ("ts", "query", "route", "routing_accuracy", "response_relevance",
                      "conciseness", "response_quality", "flags", "reflection_note"):
            self.assertIn(field, rec, msg=f"Missing field: {field}")

    def test_score_values_in_unit_interval(self):
        rec = self_eval_log.score("some query", _SPECIFIC_RESPONSE, "TechAssist")
        for axis in ("routing_accuracy", "response_relevance", "conciseness", "response_quality"):
            val = rec[axis]
            self.assertGreaterEqual(val, 0.0, msg=f"{axis} < 0")
            self.assertLessEqual(val, 1.0, msg=f"{axis} > 1")

    def test_score_with_interaction_id_appended_to_record(self):
        rec = self_eval_log.score("q", "r", interaction_id="abc123")
        self.assertEqual(rec.get("interaction_id"), "abc123")

    def test_score_written_as_valid_json_lines(self):
        self_eval_log.score("q1", "response one", "Calendar")
        self_eval_log.score("q2", "response two", "TechAssist")
        lines = self._log.read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line)  # must be valid JSON

    def test_score_never_raises_on_empty_inputs(self):
        rec = self_eval_log.score("", "", "")
        self.assertIsInstance(rec, dict)

    def test_conciseness_scores_are_rounded(self):
        rec = self_eval_log.score("should i take this job", "Yes. The team is strong and comp is competitive.", "StrategyOS")
        cs = rec["conciseness"]
        self.assertEqual(cs, round(cs, 3))


# ── Rolling average and report ────────────────────────────────────────────────

class RollingAverageTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._log = Path(self._tmpdir.name) / "self_eval.jsonl"
        self._patcher = patch(
            "harness.self_eval_log._log_path",
            return_value=self._log,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def _seed(self, n: int) -> None:
        for i in range(n):
            self_eval_log.score(f"query {i}", f"response {i} with some content here", "TechAssist")

    def test_empty_log_returns_zero_count(self):
        avg = self_eval_log.rolling_average()
        self.assertEqual(avg["count"], 0)
        self.assertIsNone(avg["response_quality"])

    def test_averages_computed_over_n_records(self):
        self._seed(10)
        avg = self_eval_log.rolling_average(n=50)
        self.assertEqual(avg["count"], 10)
        for key in ("routing_accuracy", "response_relevance", "conciseness", "response_quality"):
            self.assertIsNotNone(avg[key])
            self.assertGreaterEqual(avg[key], 0.0)
            self.assertLessEqual(avg[key], 1.0)

    def test_rolling_window_capped_at_n(self):
        self._seed(20)
        avg = self_eval_log.rolling_average(n=5)
        self.assertEqual(avg["count"], 5)

    def test_flag_counts_present(self):
        # Seed one filler-heavy response
        self_eval_log.score(
            "interview prep",
            _FILLER_RESPONSE,
            "DailyOS",
        )
        avg = self_eval_log.rolling_average()
        self.assertIsInstance(avg["top_flags"], dict)


class RoutingTagDistributionTests(unittest.TestCase):
    def test_empty_records_return_no_routes(self):
        self.assertEqual(self_eval_log.routing_tag_distribution([]), [])

    def test_counts_and_averages_quality_by_route(self):
        records = [
            {"route": "Calendar", "response_quality": 0.8},
            {"routing_tag": "Calendar", "response_quality": 0.6},
            {"route": "TechAssist", "response_quality": 0.9},
        ]

        routes = self_eval_log.routing_tag_distribution(records)

        self.assertEqual(routes[0], {
            "tag": "Calendar",
            "count": 2,
            "average_quality": 0.7,
        })
        self.assertEqual(routes[1], {
            "tag": "TechAssist",
            "count": 1,
            "average_quality": 0.9,
        })

    def test_missing_and_unsafe_tags_are_grouped_as_unknown(self):
        records = [
            {"response_quality": 0.4},
            {"route": "", "response_quality": 0.6},
            {"routing_tag": "\x1b[31mspoof", "response_quality": 0.8},
        ]

        routes = self_eval_log.routing_tag_distribution(records)

        self.assertEqual(routes, [{
            "tag": "unknown",
            "count": 3,
            "average_quality": 0.6,
        }])

    def test_distribution_is_bounded_and_preserves_total_count(self):
        records = [
            {"route": f"Route{index}", "response_quality": index / 10}
            for index in range(1, 8)
        ]

        routes = self_eval_log.routing_tag_distribution(records, limit=3)

        self.assertEqual(len(routes), 3)
        self.assertEqual(sum(route["count"] for route in routes), len(records))
        self.assertEqual(routes[-1]["tag"], "other routes")


class ScoreReportTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._log = Path(self._tmpdir.name) / "self_eval.jsonl"
        self._patcher = patch(
            "harness.self_eval_log._log_path",
            return_value=self._log,
        )
        self._patcher.start()
        self._trace_patcher = patch(
            "eval_trace_score.format_trace_score_summary",
            return_value="",
        )
        self._trace_patcher.start()

    def tearDown(self):
        self._trace_patcher.stop()
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_empty_report_says_no_data(self):
        report = self_eval_log.score_report()
        self.assertIn("No self-eval scores", report)

    def test_report_includes_quality_score(self):
        for i in range(5):
            self_eval_log.score(f"query {i}", f"response content {i}", "Calendar")
        report = self_eval_log.score_report()
        self.assertIn("Overall", report)
        self.assertIn("routing_accuracy", report)
        self.assertIn("response_relevance", report)
        self.assertIn("conciseness", report)

    def test_report_includes_bounded_route_counts_without_raw_content(self):
        records = [
            {
                "query": "PRIVATE QUERY",
                "response": "PRIVATE RESPONSE",
                "route": "Calendar",
                "routing_accuracy": 0.9,
                "response_relevance": 0.8,
                "conciseness": 0.7,
                "response_quality": 0.8,
            },
            {
                "route": "Calendar",
                "routing_accuracy": 0.7,
                "response_relevance": 0.6,
                "conciseness": 0.5,
                "response_quality": 0.6,
            },
            {
                "route": "",
                "routing_accuracy": 0.5,
                "response_relevance": 0.5,
                "conciseness": 0.5,
                "response_quality": 0.5,
            },
        ]
        self._log.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

        report = self_eval_log.score_report(n=50)

        self.assertIn("Routing tags (last 3 scored responses):", report)
        self.assertIn("Calendar: 2 responses, avg quality 0.70", report)
        self.assertIn("unknown: 1 response, avg quality 0.50", report)
        self.assertNotIn("PRIVATE QUERY", report)
        self.assertNotIn("PRIVATE RESPONSE", report)

    def test_report_shows_weakest_axis(self):
        # Force poor relevance by using error responses
        for i in range(5):
            self_eval_log.score(f"query {i}", "error: something went wrong", "Calendar")
        report = self_eval_log.score_report()
        # Should mention weakest axis
        self.assertTrue("Weakest" in report or "needs work" in report)

    def test_no_scores_above_one_in_report(self):
        for i in range(5):
            self_eval_log.score(f"q{i}", "good response here with context", "TechAssist")
        report = self_eval_log.score_report()
        # No value > 1.0 should appear
        import re
        scores_in_report = re.findall(r"\b(\d+\.\d+)\b", report)
        for s in scores_in_report:
            self.assertLessEqual(float(s), 1.0, msg=f"Score {s} exceeds 1.0 in report")


class DiagnoseReportTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._log = Path(self._tmpdir.name) / "self_eval.jsonl"
        self._patcher = patch(
            "harness.self_eval_log._log_path",
            return_value=self._log,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_empty_log_returns_no_data_message(self):
        report = self_eval_log.diagnose_report()
        self.assertIn("No self-eval scores", report)

    def test_shows_worst_n_interactions(self):
        for i in range(10):
            # Mix of high and low quality
            resp = "error: model failed" if i < 3 else _SPECIFIC_RESPONSE
            self_eval_log.score(f"query about topic {i}", resp, "Calendar")
        report = self_eval_log.diagnose_report(n=50, worst_n=3)
        # Should show 3 entries
        self.assertIn("Worst 3", report)

    def test_includes_query_and_quality(self):
        self_eval_log.score("explain recursion", "error: unavailable", "TechAssist")
        report = self_eval_log.diagnose_report(n=50, worst_n=5)
        self.assertIn("explain recursion", report)
        self.assertIn("quality=", report)

    def test_includes_flags_when_present(self):
        self_eval_log.score("should i buy this stock", "error: model down", "")
        report = self_eval_log.diagnose_report()
        # error_response flag should appear
        self.assertIn("flags:", report)

    def test_includes_route_in_output(self):
        self_eval_log.score("what's on my calendar", "error: unavailable", "Calendar")
        report = self_eval_log.diagnose_report()
        self.assertIn("Calendar", report)

    def test_includes_overall_average(self):
        for i in range(5):
            self_eval_log.score(f"query {i}", "error: fail", "DailyOS")
        report = self_eval_log.diagnose_report()
        self.assertIn("Overall avg quality", report)

    def test_worst_n_capped_at_available(self):
        self_eval_log.score("only query", "error: fail", "")
        report = self_eval_log.diagnose_report(n=50, worst_n=10)
        self.assertIn("Worst 1", report)


if __name__ == "__main__":
    unittest.main()
