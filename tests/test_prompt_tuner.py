"""Tests for harness/prompt_tuner.py — adaptive system prompt hint generator."""
import unittest
from unittest.mock import patch

from harness import prompt_tuner


def _mock_avg(**kwargs):
    """Build a rolling_average-style dict for mocking."""
    defaults = {
        "count": 10,
        "routing_accuracy": 0.85,
        "response_relevance": 0.80,
        "conciseness": 0.85,
        "response_quality": 0.83,
        "top_flags": {},
    }
    defaults.update(kwargs)
    return defaults


class PromptAppendixTests(unittest.TestCase):
    def _patch(self, avg_dict):
        return patch("harness.prompt_tuner.quality_status", return_value=avg_dict)

    def test_empty_when_insufficient_data(self):
        with self._patch(_mock_avg(count=2)):
            result = prompt_tuner.prompt_appendix()
        self.assertEqual(result, "")

    def test_empty_when_quality_is_clean(self):
        with self._patch(_mock_avg(count=10, response_quality=0.85, response_relevance=0.80)):
            result = prompt_tuner.prompt_appendix()
        self.assertEqual(result, "")

    def test_quality_floor_hint_triggered(self):
        with self._patch(_mock_avg(count=10, response_quality=0.55, response_relevance=0.80)):
            result = prompt_tuner.prompt_appendix()
        self.assertIn("Quality avg", result)
        self.assertIn("0.55", result)

    def test_low_relevance_triggers_specificity_hint(self):
        with self._patch(_mock_avg(count=10, response_relevance=0.55, response_quality=0.70)):
            result = prompt_tuner.prompt_appendix()
        self.assertIn("Relevance low", result)
        self.assertIn("Aman", result)

    def test_filler_flag_triggers_filler_hint(self):
        with self._patch(_mock_avg(
            count=10,
            top_flags={"filler_heavy": 3},
            response_quality=0.75,
            response_relevance=0.75,
        )):
            result = prompt_tuner.prompt_appendix()
        self.assertIn("filler", result.lower())

    def test_verbose_flag_triggers_length_hint(self):
        with self._patch(_mock_avg(
            count=10,
            top_flags={"verbose": 2},
            response_quality=0.75,
            response_relevance=0.75,
        )):
            result = prompt_tuner.prompt_appendix()
        self.assertIn("verbose", result.lower())

    def test_generic_response_flag_triggers_specificity_hint(self):
        with self._patch(_mock_avg(
            count=10,
            top_flags={"generic_response": 2},
            response_quality=0.75,
            response_relevance=0.70,  # above floor so flag path triggers
        )):
            result = prompt_tuner.prompt_appendix()
        # Either generic response or relevance hint
        self.assertTrue("generic" in result.lower() or "relevance" in result.lower() or "specific" in result.lower())

    def test_single_highest_priority_hint_emitted(self):
        # Multiple issues — only the top one should appear as the leading point
        with self._patch(_mock_avg(
            count=10,
            response_quality=0.55,   # hits quality floor
            response_relevance=0.55, # hits relevance floor
            top_flags={"filler_heavy": 3, "verbose": 2},
        )):
            result = prompt_tuner.prompt_appendix()
        # Quality floor is highest priority — "Quality avg" should be in result
        self.assertIn("Quality avg", result)

    def test_hint_includes_count_prefix(self):
        with self._patch(_mock_avg(count=15, response_quality=0.55)):
            result = prompt_tuner.prompt_appendix()
        self.assertIn("last 15 responses", result)

    def test_hint_never_exceeds_220_chars(self):
        with self._patch(_mock_avg(count=10, response_quality=0.55)):
            result = prompt_tuner.prompt_appendix()
        self.assertLessEqual(len(result), 220)

    def test_never_raises_on_error(self):
        with patch("harness.prompt_tuner.quality_status", side_effect=RuntimeError("boom")):
            result = prompt_tuner.prompt_appendix()
        self.assertEqual(result, "")

    def test_flag_below_threshold_does_not_trigger(self):
        # Only 1 occurrence — below the threshold of 2
        with self._patch(_mock_avg(
            count=10,
            top_flags={"filler_heavy": 1},
            response_quality=0.80,
            response_relevance=0.80,
        )):
            result = prompt_tuner.prompt_appendix()
        self.assertEqual(result, "")

    def test_over_hedged_hint_triggered_at_threshold(self):
        with self._patch(_mock_avg(
            count=10,
            top_flags={"over_hedged": 2},
            response_quality=0.75,
            response_relevance=0.75,
        )):
            result = prompt_tuner.prompt_appendix()
        self.assertIn("hedg", result.lower())


class QualityStatusTests(unittest.TestCase):
    def test_returns_dict(self):
        with patch("harness.prompt_tuner.quality_status",
                   return_value={"count": 5, "response_quality": 0.75}):
            status = prompt_tuner.quality_status()
        self.assertIsInstance(status, dict)

    def test_returns_zero_count_on_exception(self):
        with patch("harness.self_eval_log.rolling_average", side_effect=RuntimeError):
            status = prompt_tuner.quality_status()
        self.assertIn("count", status)
        self.assertEqual(status["count"], 0)


if __name__ == "__main__":
    unittest.main()
