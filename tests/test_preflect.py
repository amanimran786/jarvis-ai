import json
import os
import unittest
from unittest.mock import patch

import preflect
from task_planner import TaskStep


def _steps(*tools):
    return [TaskStep(number=i + 1, description=f"do {t}", tool=t) for i, t in enumerate(tools)]


class PreflectEnableTests(unittest.TestCase):
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JARVIS_PREFLECT", None)
            self.assertFalse(preflect.is_enabled())

    def test_enable_flag_truthiness(self):
        for val, expected in [("1", True), ("true", True), ("on", True),
                              ("0", False), ("false", False), ("", False)]:
            with patch.dict(os.environ, {"JARVIS_PREFLECT": val}):
                self.assertEqual(preflect.is_enabled(), expected, val)


class PreflectDisabledNoModelCallTests(unittest.TestCase):
    """Freeze requirement: when the flag is unset, review_plan makes NO model call."""

    def test_disabled_returns_passthrough_without_calling_critic(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JARVIS_PREFLECT", None)
            with patch("brains.brain_ollama.ask_local_structured") as mock_llm:
                result = preflect.review_plan("send an email", _steps("file", "email"))
        self.assertFalse(result.fired)
        mock_llm.assert_not_called()


class PreflectGatingTests(unittest.TestCase):
    def test_detects_side_effecting_tools(self):
        has_risky, risky = preflect._plan_has_irreversible_step(_steps("file", "terminal"))
        self.assertTrue(has_risky)
        self.assertIn("file", risky)
        self.assertIn("terminal", risky)

    def test_readonly_plan_is_not_risky(self):
        has_risky, risky = preflect._plan_has_irreversible_step(_steps("search", "research"))
        self.assertFalse(has_risky)
        self.assertEqual(risky, [])

    def test_enabled_readonly_plan_skips_critic(self):
        with patch.dict(os.environ, {"JARVIS_PREFLECT": "1"}):
            with patch("brains.brain_ollama.ask_local_structured") as mock_llm, \
                 patch("infra.security_audit.audit_event") as mock_audit:
                result = preflect.review_plan("look something up", _steps("search"))
        self.assertFalse(result.fired)
        mock_llm.assert_not_called()
        mock_audit.assert_not_called()


class PreflectCritiqueTests(unittest.TestCase):
    def test_risky_plan_calls_critic_and_audits(self):
        verdict_json = json.dumps({
            "verdict": "revise",
            "risks": ["irreversible-without-guard: email send with no confirmation"],
            "summary": "email step lacks a confirmation/rollback",
        })
        with patch.dict(os.environ, {"JARVIS_PREFLECT": "1"}):
            with patch("brains.brain_ollama.ask_local_structured", return_value=verdict_json), \
                 patch("infra.security_audit.audit_event") as mock_audit:
                result = preflect.review_plan("email the report", _steps("file", "email"),
                                              task_id="run_abc")
        self.assertTrue(result.fired)
        self.assertEqual(result.verdict, "revise")
        self.assertTrue(result.risks)
        mock_audit.assert_called_once()
        kwargs = mock_audit.call_args.kwargs
        self.assertEqual(kwargs["action"], "plan_precheck")
        self.assertEqual(kwargs["decision"], "revise")
        self.assertEqual(kwargs["severity"], "warning")
        self.assertEqual(kwargs["task_id"], "run_abc")

    def test_malformed_critic_output_fails_open(self):
        with patch.dict(os.environ, {"JARVIS_PREFLECT": "1"}):
            with patch("brains.brain_ollama.ask_local_structured", return_value="not json{"), \
                 patch("infra.security_audit.audit_event") as mock_audit:
                result = preflect.review_plan("write a file", _steps("file"))
        # Fired (a risky step existed) but defaults safe and never raises.
        self.assertTrue(result.fired)
        self.assertEqual(result.verdict, "approve")
        mock_audit.assert_called_once()
        self.assertEqual(mock_audit.call_args.kwargs["severity"], "info")

    def test_approve_verdict_audits_info(self):
        verdict_json = json.dumps({"verdict": "approve", "risks": [], "summary": "plan reads before writing"})
        with patch.dict(os.environ, {"JARVIS_PREFLECT": "1"}):
            with patch("brains.brain_ollama.ask_local_structured", return_value=verdict_json), \
                 patch("infra.security_audit.audit_event") as mock_audit:
                result = preflect.review_plan("save notes", _steps("file"))
        self.assertTrue(result.fired)
        self.assertEqual(result.verdict, "approve")
        self.assertEqual(mock_audit.call_args.kwargs["severity"], "info")


if __name__ == "__main__":
    unittest.main()
