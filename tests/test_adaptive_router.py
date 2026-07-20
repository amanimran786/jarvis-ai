"""Tests for harness/adaptive_router.py — route quality tracking and demotion."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import adaptive_router
from harness.adaptive_router import RouteStats


def _patch_paths(tmpdir: Path):
    return [
        patch("harness.adaptive_router._self_eval_log_path",
              return_value=tmpdir / "logs" / "self_eval.jsonl"),
        patch("harness.adaptive_router._quality_store_path",
              return_value=tmpdir / "logs" / "route_quality.json"),
    ]


class RouteStatsTests(unittest.TestCase):
    def test_avg_quality_none_when_no_data(self):
        rs = RouteStats(route="Calendar")
        self.assertIsNone(rs.avg_quality)

    def test_avg_quality_computed_correctly(self):
        rs = RouteStats(route="Calendar")
        rs.record(0.80)
        rs.record(0.60)
        self.assertAlmostEqual(rs.avg_quality, 0.70, places=2)

    def test_not_demoted_below_min_samples(self):
        rs = RouteStats(route="Calendar")
        for _ in range(4):   # < min_samples=5
            rs.record(0.30)
        self.assertFalse(rs.is_demoted())

    def test_demoted_when_low_quality_and_enough_samples(self):
        rs = RouteStats(route="Calendar")
        for _ in range(5):
            rs.record(0.50)  # below threshold 0.65
        self.assertTrue(rs.is_demoted())

    def test_not_demoted_when_high_quality(self):
        rs = RouteStats(route="Knowledge")
        for _ in range(10):
            rs.record(0.85)
        self.assertFalse(rs.is_demoted())

    def test_to_dict_has_expected_keys(self):
        rs = RouteStats(route="Status")
        rs.record(0.70)
        d = rs.to_dict()
        for k in ("route", "count", "avg_quality", "demoted", "recent"):
            self.assertIn(k, d)

    def test_recent_maxlen_is_20(self):
        rs = RouteStats(route="X")
        for i in range(30):
            rs.record(float(i) / 30)
        self.assertLessEqual(len(rs.recent), 20)


class DemotionLogicTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        base = Path(self._tmpdir.name)
        (base / "logs").mkdir(parents=True)
        self._patches = _patch_paths(base)
        for p in self._patches:
            p.start()
        # Reset module state
        adaptive_router._stats.clear()
        adaptive_router._demoted = frozenset()
        adaptive_router._last_refresh = 0.0
        adaptive_router._pending_outcomes.clear()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()

    def _seed_stats(self, route: str, quality: float, n: int = 5):
        rs = RouteStats(route=route)
        for _ in range(n):
            rs.record(quality)
        adaptive_router._stats[route] = rs

    def test_is_demoted_false_for_unknown_route(self):
        self.assertFalse(adaptive_router.is_demoted("UnknownRoute"))

    def test_is_demoted_false_below_min_samples(self):
        self._seed_stats("Knowledge", quality=0.30, n=2)  # < 5
        self.assertFalse(adaptive_router.is_demoted("Knowledge"))

    def test_is_demoted_true_when_low_quality(self):
        self._seed_stats("Knowledge", quality=0.40, n=6)
        self.assertTrue(adaptive_router.is_demoted("Knowledge"))

    def test_is_demoted_false_when_high_quality(self):
        self._seed_stats("Knowledge", quality=0.85, n=6)
        self.assertFalse(adaptive_router.is_demoted("Knowledge"))

    def test_side_effect_route_classification(self):
        for route in ("Calendar", "Gmail", "Messages", "Browser", "Meeting"):
            self.assertTrue(adaptive_router.is_side_effect_route(route))

    def test_non_side_effect_route(self):
        for route in ("Knowledge", "Vault", "Status", "Self-Eval"):
            self.assertFalse(adaptive_router.is_side_effect_route(route))

    def test_should_fallback_requires_safe_route(self):
        self._seed_stats("Calendar", quality=0.30, n=6)
        # Calendar is a side-effect route — should NOT fallback
        self.assertFalse(adaptive_router.should_fallback("Calendar"))

    def test_should_fallback_true_for_demoted_safe_route(self):
        self._seed_stats("Knowledge", quality=0.30, n=6)
        self.assertTrue(adaptive_router.should_fallback("Knowledge"))

    def test_should_fallback_false_for_good_route(self):
        self._seed_stats("Knowledge", quality=0.85, n=6)
        self.assertFalse(adaptive_router.should_fallback("Knowledge"))

    def test_get_demoted_routes_reflects_stats(self):
        self._seed_stats("Status", quality=0.30, n=6)
        self._seed_stats("Knowledge", quality=0.30, n=6)
        self._seed_stats("Calendar", quality=0.30, n=6)
        adaptive_router._demoted = frozenset(
            r for r, rs in adaptive_router._stats.items() if rs.is_demoted()
        )
        demoted = adaptive_router.get_demoted_routes()
        self.assertIn("Status", demoted)
        self.assertIn("Knowledge", demoted)
        self.assertIn("Calendar", demoted)  # calendar still tracked even if unsafe to bypass

    def test_record_quality_updates_stats(self):
        with patch("harness.adaptive_router._maybe_refresh"):
            adaptive_router.record_quality("Status", 0.40)
        self.assertIn("Status", adaptive_router._stats)
        self.assertEqual(adaptive_router._stats["Status"].count, 1)

    def test_record_quality_blank_route_ignored(self):
        with patch("harness.adaptive_router._maybe_refresh"):
            adaptive_router.record_quality("", 0.40)
        self.assertNotIn("", adaptive_router._stats)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        base = Path(self._tmpdir.name)
        (base / "logs").mkdir(parents=True)
        self._base = base
        self._patches = _patch_paths(base)
        for p in self._patches:
            p.start()
        adaptive_router._stats.clear()
        adaptive_router._demoted = frozenset()
        adaptive_router._last_refresh = 0.0
        adaptive_router._pending_outcomes.clear()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()

    def test_save_and_load_persisted(self):
        rs = RouteStats(route="Knowledge")
        for _ in range(6):
            rs.record(0.40)
        adaptive_router._stats["Knowledge"] = rs
        adaptive_router._save_persisted(adaptive_router._stats)

        path = self._base / "logs" / "route_quality.json"
        self.assertTrue(path.exists())
        data = json.loads(path.read_text())
        self.assertEqual(data[0]["route"], "Knowledge")
        self.assertEqual(data[0]["count"], 6)
        self.assertTrue(data[0]["demoted"])

    def test_load_persisted_restores_stats(self):
        # Write a quality file directly
        path = self._base / "logs" / "route_quality.json"
        path.write_text(json.dumps([
            {"route": "Vault", "count": 8, "avg_quality": 0.45,
             "avg_routing": 0.50, "avg_relevance": 0.45,
             "demoted": True, "recent": [0.45] * 8},
        ]))
        loaded = adaptive_router._load_persisted()
        self.assertIn("Vault", loaded)
        self.assertEqual(loaded["Vault"].count, 8)

    def test_empty_json_returns_empty_dict(self):
        path = self._base / "logs" / "route_quality.json"
        path.write_text("not-valid-json")
        result = adaptive_router._load_persisted()
        self.assertEqual(result, {})


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        base = Path(self._tmpdir.name)
        (base / "logs").mkdir(parents=True)
        self._log = base / "logs" / "self_eval.jsonl"
        self._patches = _patch_paths(base)
        for p in self._patches:
            p.start()
        adaptive_router._stats.clear()
        adaptive_router._demoted = frozenset()
        adaptive_router._last_refresh = 0.0
        adaptive_router._pending_outcomes.clear()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()

    def _seed_log(self, route: str, quality: float, n: int = 6):
        from datetime import datetime, timezone, timedelta
        with open(self._log, "a") as f:
            for i in range(n):
                ts = (datetime.now(timezone.utc) - timedelta(hours=n - i)).isoformat()
                record = {
                    "ts": ts, "query": f"query {i}", "route": route,
                    "routing_accuracy": 0.50, "response_relevance": 0.45,
                    "conciseness": 0.80, "response_quality": quality, "flags": [],
                }
                f.write(json.dumps(record) + "\n")

    def test_refresh_loads_demoted_from_log(self):
        self._seed_log("Knowledge", quality=0.40, n=6)
        with patch("harness.adaptive_router._load_persisted", return_value={}), \
             patch("harness.self_eval_log._log_path", return_value=self._log):
            adaptive_router.refresh()
        self.assertIn("Knowledge", adaptive_router._demoted)

    def test_refresh_clears_pending_outcomes(self):
        adaptive_router._pending_outcomes.append(("Status", 0.40, 0.50, 0.45))
        with patch("harness.adaptive_router._load_persisted", return_value={}), \
             patch("harness.adaptive_router._load_from_self_eval", return_value={}), \
             patch("harness.adaptive_router._save_persisted"):
            adaptive_router.refresh()
        self.assertEqual(adaptive_router._pending_outcomes, [])

    def test_refresh_does_not_demote_with_few_samples(self):
        self._seed_log("Knowledge", quality=0.30, n=3)  # < min_samples=5
        with patch("harness.adaptive_router._load_persisted", return_value={}):
            adaptive_router.refresh()
        self.assertNotIn("Knowledge", adaptive_router._demoted)


class FallbackStreamTests(unittest.TestCase):
    def test_fallback_stream_returns_generator(self):
        import inspect
        gen = adaptive_router.build_fallback_stream("hello", "Status")
        self.assertTrue(inspect.isgenerator(gen))

    def test_fallback_stream_yields_on_model_error(self):
        with patch("brains.brain_ollama.ask_local", side_effect=RuntimeError("down")), \
             patch("brains.brain_ollama.get_best_available", return_value="test-model"):
            gen = adaptive_router.build_fallback_stream("hello", "Status")
            result = next(gen)
        self.assertIn("Fallback failed", result)

    def test_fallback_stream_yields_response(self):
        with patch("brains.brain_ollama.ask_local", return_value="Direct answer."), \
             patch("brains.brain_ollama.get_best_available", return_value="test-model"):
            gen = adaptive_router.build_fallback_stream("what is X?", "Status")
            result = next(gen)
        self.assertEqual(result, "Direct answer.")


class RouteQualityReportTests(unittest.TestCase):
    def setUp(self):
        adaptive_router._stats.clear()
        adaptive_router._demoted = frozenset()

    def test_no_data_returns_informative_message(self):
        with patch("harness.adaptive_router.refresh"):
            result = adaptive_router.route_quality_report()
        self.assertIn("No route quality data", result)

    def test_demoted_route_flagged_in_report(self):
        rs = RouteStats(route="Knowledge")
        for _ in range(6):
            rs.record(0.40)
        adaptive_router._stats["Knowledge"] = rs
        adaptive_router._demoted = frozenset({"Knowledge"})
        with patch("harness.adaptive_router.refresh"):
            report = adaptive_router.route_quality_report()
        self.assertIn("DEMOTED", report)
        self.assertIn("Knowledge", report)

    def test_side_effect_route_labeled_in_report(self):
        rs = RouteStats(route="Calendar")
        for _ in range(6):
            rs.record(0.40)
        adaptive_router._stats["Calendar"] = rs
        adaptive_router._demoted = frozenset({"Calendar"})
        with patch("harness.adaptive_router.refresh"):
            report = adaptive_router.route_quality_report()
        self.assertIn("side-effect", report)


if __name__ == "__main__":
    unittest.main()
