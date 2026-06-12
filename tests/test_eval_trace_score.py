import unittest

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


if __name__ == "__main__":
    unittest.main()
