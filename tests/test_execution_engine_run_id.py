import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import execution_engine
from task_planner import TaskStep


class ExecutionRunIdTraceTests(unittest.TestCase):
    def test_execute_step_records_run_id_and_actual_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp)
            step = TaskStep(
                number=1,
                description="search web",
                tool="search",
                params={"query": "jarvis"},
            )
            with patch.object(execution_engine, "TRACE_DIR", trace_dir), \
                 patch("execution_engine._execute_tool_call", return_value=(True, "search output")):
                ok, result = execution_engine.execute_step(step, {}, run_id="run_test")

            self.assertTrue(ok)
            self.assertEqual(result, "search output")
            payload = json.loads(next(trace_dir.glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(payload["run_id"], "run_test")
            self.assertEqual(payload["attempts"], 1)

    def test_precheck_failure_records_run_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp)
            step = TaskStep(
                number=1,
                description="bad search",
                tool="search",
                params={"max_results": "not-an-int"},
            )
            with patch.object(execution_engine, "TRACE_DIR", trace_dir):
                ok, _ = execution_engine.execute_step(step, {}, run_id="run_bad")

            self.assertFalse(ok)
            payload = json.loads(next(trace_dir.glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(payload["run_id"], "run_bad")


if __name__ == "__main__":
    unittest.main()
