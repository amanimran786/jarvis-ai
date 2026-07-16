import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import eval_trace_score as score


class EvalTraceScoreTests(unittest.TestCase):
    def test_score_run_computes_expected_metrics_from_memory(self):
        run = [
            {"run_id": "r1", "step_number": 1, "tool": "search", "ok": True, "attempts": 1, "elapsed_ms": 10},
            {"run_id": "r1", "step_number": 2, "tool": "file", "ok": True, "attempts": 1, "elapsed_ms": 5},
        ]

        card = score.score_run("r1", expected_tools=["search", "file"], min_steps=2, run=run)

        self.assertEqual(card["run_id"], "r1")
        self.assertEqual(card["steps"], 2)
        self.assertEqual(card["tool_correctness"]["score"], 1.0)
        self.assertEqual(card["step_efficiency"]["score"], 1.0)
        self.assertEqual(card["plan_adherence"]["score"], 1.0)
        self.assertEqual(card["latency"]["total_ms"], 15)

    def test_step_efficiency_penalizes_actual_retries(self):
        run = [
            {"tool": "search", "ok": True, "attempts": 2, "elapsed_ms": 10},
            {"tool": "file", "ok": True, "attempts": 1, "elapsed_ms": 5},
        ]

        result = score.step_efficiency(run, min_steps=2)

        self.assertEqual(result["retries"], 1)
        self.assertEqual(result["score"], 0.5)

    def test_bounded_summary_is_observe_only(self):
        aggregate = {
            "total_runs": 3,
            "total_steps": 12,
            "total_failed_steps": 1,
            "total_retried_steps": 2,
            "avg_step_efficiency": 0.833,
            "total_elapsed_ms": 250,
            "runs": {},
        }

        with patch.object(score, "aggregate_trace_score", return_value=aggregate) as mocked:
            summary = score.format_trace_score_summary(last_n=50)

        mocked.assert_called_once_with(last_n=50)
        self.assertIn("observe only", summary.lower())
        self.assertIn("3 runs", summary)
        self.assertIn("12 steps", summary)
        self.assertIn("92% step success", summary)
        self.assertIn("83% average efficiency", summary)
        self.assertIn("2 retries", summary)

    def test_bounded_summary_is_empty_without_trace_data(self):
        with patch.object(
            score,
            "aggregate_trace_score",
            return_value={"total_runs": 0, "runs": {}},
        ):
            self.assertEqual(score.format_trace_score_summary(last_n=50), "")

    def test_aggregate_last_n_uses_latest_trace_activity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            trace_dir = Path(tmpdir)
            (trace_dir / "20260101_000000_z-old.json").write_text(
                json.dumps({
                    "run_id": "z-old",
                    "step_number": 1,
                    "tool": "search",
                    "ok": True,
                    "attempts": 1,
                    "elapsed_ms": 5,
                }),
                encoding="utf-8",
            )
            (trace_dir / "20260102_000000_a-latest.json").write_text(
                json.dumps({
                    "run_id": "a-latest",
                    "step_number": 1,
                    "tool": "file",
                    "ok": True,
                    "attempts": 1,
                    "elapsed_ms": 7,
                }),
                encoding="utf-8",
            )

            with patch.object(score, "TRACE_DIR", trace_dir):
                report = score.aggregate_trace_score(last_n=1)

        self.assertEqual(list(report["runs"]), ["a-latest"])

    def test_aggregate_reads_each_trace_file_once(self):
        paths = []
        for index, run_id in enumerate(("run-c", "run-a", "run-b"), 1):
            path = MagicMock()
            path.read_text.return_value = json.dumps({
                "run_id": run_id,
                "step_number": 1,
                "tool": "search",
                "ok": True,
                "attempts": 1,
                "elapsed_ms": index,
            })
            paths.append(path)

        with patch.object(score, "_iter_trace_files", return_value=paths):
            score.aggregate_trace_score(last_n=2)

        self.assertEqual(sum(path.read_text.call_count for path in paths), len(paths))

    def test_score_and_briefing_use_the_same_trace_summary(self):
        import briefing
        import jarvis_agents
        from harness import self_eval_log

        trace_summary = (
            "Execution traces (observe only, last 50): 3 runs, 12 steps, "
            "92% step success, 83% average efficiency, 2 retries."
        )
        empty_self_eval = {
            "count": 0,
            "routing_accuracy": None,
            "response_relevance": None,
            "conciseness": None,
            "response_quality": None,
            "top_flags": {},
        }

        with patch.object(score, "format_trace_score_summary", return_value=trace_summary), \
             patch.object(self_eval_log, "rolling_average", return_value=empty_self_eval):
            score_output = self_eval_log.score_report()

        with patch.object(score, "format_trace_score_summary", return_value=trace_summary), \
             patch.object(briefing, "_fetch_parallel", return_value={}), \
             patch.object(briefing, "_greeting", return_value="Good morning, Aman."):
            briefing_output = briefing.build_briefing()

        self.assertIn(trace_summary, score_output)
        self.assertIn(f"**Agent execution:** {trace_summary}", briefing_output)

        graph = MagicMock()
        graph.stage.return_value = graph
        graph.run.return_value = []
        with patch.object(score, "format_trace_score_summary", return_value=trace_summary), \
             patch.object(jarvis_agents, "AgentGraph", return_value=graph), \
             patch.object(jarvis_agents, "_merge_results", return_value="Raw briefing data."), \
             patch.object(jarvis_agents, "_synthesise", return_value="Spoken briefing."):
            packaged_output = jarvis_agents.run_briefing()

        self.assertEqual(packaged_output, f"Spoken briefing.\n\n{trace_summary}")


if __name__ == "__main__":
    unittest.main()
