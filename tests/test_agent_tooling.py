import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import execution_engine
import task_planner
import tool_registry


class ToolRegistryTests(unittest.TestCase):
    def test_validate_args_enforces_required_fields(self):
        ok, normalized, error = tool_registry.validate_args("terminal", {})
        self.assertFalse(ok)
        self.assertEqual(normalized, {})
        self.assertIn("Missing required argument", error)

    def test_validate_args_normalizes_types(self):
        ok, normalized, error = tool_registry.validate_args(
            "malware_list_samples",
            {"limit": "10", "status": "open"},
        )
        self.assertTrue(ok, error)
        self.assertEqual(normalized["limit"], 10)

    def test_callable_summary_includes_malware_tools(self):
        text = tool_registry.callable_tool_summaries()
        self.assertIn("malware_get_alert", text)
        self.assertIn("malware_submit_hash", text)

    def test_callable_summary_includes_osint_tools(self):
        text = tool_registry.callable_tool_summaries()
        self.assertIn("osint_username", text)
        self.assertIn("osint_domain_typos", text)

    def test_validate_args_normalizes_bool_strings(self):
        ok, normalized, error = tool_registry.validate_args(
            "osint_domain_typos",
            {"domain": "example.com", "registered_only": "false"},
        )
        self.assertTrue(ok, error)
        self.assertIs(normalized["registered_only"], False)


class ExecutionEngineContractTests(unittest.TestCase):
    def test_execute_step_records_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp)
            step = task_planner.TaskStep(number=1, description="search web", tool="search", params={"query": "jarvis"})
            with patch.object(execution_engine, "TRACE_DIR", trace_dir), \
                 patch("execution_engine._execute_tool_call", return_value=(True, "search output")):
                ok, result = execution_engine.execute_step(step, {})
            self.assertTrue(ok)
            self.assertEqual(result, "search output")
            files = list(trace_dir.glob("*.json"))
            self.assertEqual(len(files), 1)
            payload = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["tool"], "search")
            self.assertTrue(payload["ok"])

    def test_execute_step_retries_idempotent_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp)
            step = task_planner.TaskStep(number=1, description="search web", tool="search", params={"query": "jarvis"})
            responses = [(False, "temporary error"), (True, "ok result")]

            def side_effect(*_args, **_kwargs):
                return responses.pop(0)

            with patch.object(execution_engine, "TRACE_DIR", trace_dir), \
                 patch("execution_engine._execute_tool_call", side_effect=side_effect):
                ok, result = execution_engine.execute_step(step, {})
            self.assertTrue(ok)
            self.assertEqual(result, "ok result")
            payload = json.loads(next(trace_dir.glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(payload["attempts"], 2)

    def test_email_tool_send_is_confirmation_gated(self):
        step = task_planner.TaskStep(
            number=1,
            description="send email",
            tool="email",
            params={"action": "send", "to": "beta@example.com", "subject": "Beta", "body": "Ship it"},
        )
        with patch("google_services.send_email") as send_mock:
            ok, result = execution_engine._execute_tool_call("email", step.params, step, {})
        self.assertFalse(ok)
        self.assertIn("confirmation draft", result.lower())
        send_mock.assert_not_called()


class TaskPlannerSanitizerTests(unittest.TestCase):
    def test_plan_task_downgrades_unknown_tools_to_chat(self):
        response = json.dumps(
            [
                {"number": 1, "description": "do unknown thing", "tool": "foobar_tool", "params": {"x": 1}},
                {"number": 2, "description": "search", "tool": "search", "params": {"query": "jarvis"}},
            ]
        )
        with patch("task_planner.ask_claude", return_value=response):
            steps = task_planner.plan_task("test plan")
        self.assertEqual(steps[0].tool, "chat")
        self.assertEqual(steps[1].tool, "search")


if __name__ == "__main__":
    unittest.main()
