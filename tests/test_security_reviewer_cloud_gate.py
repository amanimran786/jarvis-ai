"""
Cloud security-review fallback must be opt-in.

Review payloads contain task content and code — they must never leave the
machine on a silent default path. The fallback requires
JARVIS_ALLOW_CLOUD_SECURITY_REVIEW to be explicitly enabled AND a mode that
permits cloud calls (not open-source). Without it, the gate fails closed:
empty LLM output parses to _FALLBACK_VERDICT (FAIL/critical, manual review).
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

from agents import security_reviewer as sr


def _env(**overrides):
    """patch.dict os.environ with a clean provider/flag slate plus overrides."""
    base = {
        "JARVIS_ALLOW_CLOUD_SECURITY_REVIEW": "",
        "GEMINI_API_KEY": "",
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
    }
    base.update(overrides)
    return patch.dict(os.environ, base)


class CloudGateAllowedTests(unittest.TestCase):
    def test_denied_without_flag_even_with_provider_keys(self):
        with _env(GEMINI_API_KEY="k", OPENAI_API_KEY="k", ANTHROPIC_API_KEY="k"):
            self.assertFalse(sr._cloud_security_review_allowed())

    def test_denied_when_flag_set_but_open_source_mode(self):
        with _env(JARVIS_ALLOW_CLOUD_SECURITY_REVIEW="1"), \
             patch("model_router.is_open_source_mode", return_value=True):
            self.assertFalse(sr._cloud_security_review_allowed())

    def test_allowed_with_flag_and_non_open_source_mode(self):
        with _env(JARVIS_ALLOW_CLOUD_SECURITY_REVIEW="1"), \
             patch("model_router.is_open_source_mode", return_value=False):
            self.assertTrue(sr._cloud_security_review_allowed())

    def test_flag_must_be_truthy(self):
        for value in ("0", "false", "no", "off", " "):
            with _env(JARVIS_ALLOW_CLOUD_SECURITY_REVIEW=value), \
                 patch("model_router.is_open_source_mode", return_value=False):
                self.assertFalse(
                    sr._cloud_security_review_allowed(),
                    f"flag value {value!r} must not enable cloud review",
                )


class CloudReviewCallTests(unittest.TestCase):
    """Prove no provider client is constructed unless the gate allows it."""

    def _gemini_modules(self):
        genai_mod = MagicMock()
        client = genai_mod.Client.return_value
        client.models.generate_content.return_value = MagicMock(
            text='{"verdict": "PASS", "severity": "none", "findings": [], "summary": "ok"}'
        )
        google_pkg = MagicMock()
        google_pkg.genai = genai_mod
        modules = {
            "google": google_pkg,
            "google.genai": genai_mod,
            "google.genai.types": genai_mod.types,
        }
        return genai_mod, modules

    def test_no_provider_call_when_gate_denied(self):
        genai_mod, modules = self._gemini_modules()
        with _env(GEMINI_API_KEY="k"), patch.dict(sys.modules, modules):
            result = sr._cloud_security_review('{"prompt": "review this"}')
        self.assertEqual(result, "")
        genai_mod.Client.assert_not_called()

    def test_provider_called_when_gate_allows(self):
        genai_mod, modules = self._gemini_modules()
        roster = {"security_reviewer": {"system_prompt": "You are a security reviewer."}}
        with _env(JARVIS_ALLOW_CLOUD_SECURITY_REVIEW="1", GEMINI_API_KEY="k"), \
             patch("model_router.is_open_source_mode", return_value=False), \
             patch("config.AGENT_ROSTER", roster), \
             patch.dict(sys.modules, modules):
            result = sr._cloud_security_review('{"prompt": "review this"}')
        self.assertIn('"verdict"', result)
        genai_mod.Client.assert_called_once()


class ReviewFailsClosedTests(unittest.TestCase):
    def test_empty_local_output_with_gate_denied_yields_manual_review_fail(self):
        with _env(GEMINI_API_KEY="k", OPENAI_API_KEY="k", ANTHROPIC_API_KEY="k"), \
             patch("brains.brain_ollama.ask_local_structured", return_value=""):
            verdict = sr.review({"prompt": "benign payload"}, task_id="t1", stage=0)
        self.assertEqual(verdict.verdict, "FAIL")
        self.assertEqual(verdict.severity, "critical")
        self.assertTrue(verdict.is_blocking())


_GOOD_VERDICT_JSON = (
    '{"verdict": "PASS", "severity": "none", "findings": [], '
    '"summary": "No threats detected."}'
)


class LocalStructuredPathTests(unittest.TestCase):
    """Item 9 root-cause fix: verdicts come from the schema-constrained local
    call — no tool loop, no markdown/think stripping in the path."""

    def test_structured_verdict_parsed_and_cloud_never_consulted(self):
        with _env(GEMINI_API_KEY="k"), \
             patch("brains.brain_ollama.ask_local_structured",
                   return_value=_GOOD_VERDICT_JSON) as local_mock, \
             patch.object(sr, "_cloud_security_review") as cloud_mock:
            verdict = sr.review({"prompt": "benign payload"}, task_id="t9", stage=0)
        self.assertEqual(verdict.verdict, "PASS")
        self.assertEqual(verdict.severity, "none")
        local_mock.assert_called_once()
        cloud_mock.assert_not_called()
        # schema is passed so JSON is grammar-enforced at generation time
        self.assertEqual(local_mock.call_args.args[1], sr._VERDICT_SCHEMA)

    def test_structured_path_exception_fails_closed(self):
        with _env(), \
             patch("brains.brain_ollama.ask_local_structured",
                   side_effect=RuntimeError("ollama read timeout")):
            verdict = sr.review({"prompt": "benign payload"}, task_id="t9", stage=0)
        self.assertEqual(verdict.verdict, "FAIL")
        self.assertTrue(verdict.is_blocking())


if __name__ == "__main__":
    unittest.main()
