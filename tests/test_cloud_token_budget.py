"""JARVIS_CLOUD_TOKENS_PER_HOUR rate guard (flag-gated, default off).

When set to a positive integer and the last hour's cloud token usage meets it,
auto-mode routing degrades to local instead of bursting into provider rate
limits. Strict local-first may already keep non-high-stakes work local before
the cloud budget guard is needed.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import model_router
import provider_router
import usage_tracker

AGENT_PROMPT = (
    "You are agent backend-engineer. Analyze the task, review the codebase, "
    "and write a comprehensive debrief of what you executed and verified."
)

_CLOUD_HEAVY = [{"local": False, "total_tokens": 500_000}]


class BudgetHelperTests(unittest.TestCase):
    def test_unset_flag_is_off_even_with_heavy_usage(self):
        with patch.dict("os.environ", {}, clear=False), \
             patch.object(usage_tracker, "entries", return_value=_CLOUD_HEAVY):
            import os
            os.environ.pop("JARVIS_CLOUD_TOKENS_PER_HOUR", None)
            self.assertFalse(model_router._cloud_token_budget_exhausted())

    def test_garbage_flag_is_off(self):
        with patch.dict("os.environ", {"JARVIS_CLOUD_TOKENS_PER_HOUR": "lots"}), \
             patch.object(usage_tracker, "entries", return_value=_CLOUD_HEAVY):
            self.assertFalse(model_router._cloud_token_budget_exhausted())

    def test_under_budget_is_off(self):
        rows = [{"local": False, "total_tokens": 100}]
        with patch.dict("os.environ", {"JARVIS_CLOUD_TOKENS_PER_HOUR": "1000"}), \
             patch.object(usage_tracker, "entries", return_value=rows):
            self.assertFalse(model_router._cloud_token_budget_exhausted())

    def test_over_budget_trips(self):
        with patch.dict("os.environ", {"JARVIS_CLOUD_TOKENS_PER_HOUR": "1000"}), \
             patch.object(usage_tracker, "entries", return_value=_CLOUD_HEAVY):
            self.assertTrue(model_router._cloud_token_budget_exhausted())

    def test_local_usage_does_not_count(self):
        rows = [{"local": True, "total_tokens": 500_000}]
        with patch.dict("os.environ", {"JARVIS_CLOUD_TOKENS_PER_HOUR": "1000"}), \
             patch.object(usage_tracker, "entries", return_value=rows):
            self.assertFalse(model_router._cloud_token_budget_exhausted())


class BudgetRoutingTests(unittest.TestCase):
    def _plan_for(self, env: dict, prompt: str = AGENT_PROMPT):
        captured = {}
        real_build_plan = provider_router.build_plan

        def spy_build_plan(**kwargs):
            captured.update(kwargs)
            return real_build_plan(**kwargs)

        with patch.dict("os.environ", env), \
             patch.object(usage_tracker, "entries", return_value=_CLOUD_HEAVY), \
             patch.object(model_router, "_current_mode", "auto"), \
             patch.object(model_router, "_is_mobile_web_active", return_value=False), \
             patch.object(model_router, "forced_model_status", return_value={"active": False}), \
             patch.object(model_router, "_has_local", return_value=True), \
             patch.object(model_router, "_best_local", return_value="jarvis-local"), \
             patch.object(model_router.provider_router, "build_plan", spy_build_plan):
            # Lazy stream — never iterated, no model call happens.
            model_router.smart_stream(prompt, tool="chat")
        return captured

    def test_strict_local_first_keeps_non_high_stakes_agent_work_local(self):
        kwargs = self._plan_for({})
        self.assertEqual(kwargs["tier"], "sonnet")
        self.assertFalse(kwargs["explicit_cloud"])
        plan = provider_router.build_plan(**kwargs)
        self.assertTrue(plan.candidates[0].local)

    def test_exhausted_budget_degrades_explicit_cloud_route_to_local(self):
        kwargs = self._plan_for(
            {"JARVIS_CLOUD_TOKENS_PER_HOUR": "1000"},
            prompt="I have a production security vulnerability. Give me a comprehensive incident plan.",
        )
        self.assertEqual(kwargs["tier"], "local")
        self.assertFalse(kwargs["explicit_cloud"])
        plan = provider_router.build_plan(**kwargs)
        self.assertTrue(plan.candidates[0].local)

    def test_flag_unset_keeps_high_stakes_cloud_route_unchanged(self):
        import os
        os.environ.pop("JARVIS_CLOUD_TOKENS_PER_HOUR", None)
        kwargs = self._plan_for(
            {},
            prompt="I have a production security vulnerability. Give me a comprehensive incident plan.",
        )
        self.assertEqual(kwargs["tier"], "sonnet")
        self.assertTrue(kwargs["explicit_cloud"])


class GlobalBudgetCandidateGateTests(unittest.TestCase):
    """The global hourly cap must gate every cloud candidate at execution time,
    not just the auto-mode explicit-cloud plan gate. Usage here (2k tokens) is
    far below the per-provider 100k/hr hard limit in harness/budget.py, so only
    the JARVIS_CLOUD_TOKENS_PER_HOUR check can cause the cloud skip."""

    def test_exhausted_global_budget_skips_cloud_candidate_and_uses_local(self):
        rows = [{"local": False, "total_tokens": 2_000}]
        cloud = provider_router.RouteCandidate(
            provider="openai", model="gpt-4o-mini", local=False, label="GPT-mini",
        )
        local = provider_router.RouteCandidate(
            provider="ollama", model="jarvis-local", local=True, label="Local",
        )
        plan = provider_router.RoutePlan(
            mode="auto", tier="mini", candidates=(cloud, local), reason="test",
        )

        cloud_called = []

        def fake_cloud_stream(*args, **kwargs):
            cloud_called.append(True)
            yield "cloud"

        def fake_local_stream(*args, **kwargs):
            yield "local-ok"

        with patch.dict("os.environ", {"JARVIS_CLOUD_TOKENS_PER_HOUR": "1000"}), \
             patch.object(usage_tracker, "entries", return_value=rows), \
             patch.object(model_router, "_current_mode", "auto"), \
             patch.object(model_router, "_is_mobile_web_active", return_value=False), \
             patch.object(model_router, "forced_model_status", return_value={"active": False}), \
             patch.object(model_router, "_has_local", return_value=True), \
             patch.object(model_router, "_best_local", return_value="jarvis-local"), \
             patch.object(model_router.provider_router, "build_plan", return_value=plan), \
             patch.object(model_router, "ask_stream", side_effect=fake_cloud_stream), \
             patch.object(model_router, "ask_local_stream", side_effect=fake_local_stream):
            stream, label = model_router.smart_stream("hello there friend", tool="chat")
            output = "".join(stream)

        self.assertEqual(cloud_called, [])
        self.assertIn("local-ok", output)

    def test_under_global_budget_cloud_candidate_still_runs(self):
        rows = [{"local": False, "total_tokens": 100}]
        cloud = provider_router.RouteCandidate(
            provider="openai", model="gpt-4o-mini", local=False, label="GPT-mini",
        )
        plan = provider_router.RoutePlan(
            mode="auto", tier="mini", candidates=(cloud,), reason="test",
        )

        def fake_cloud_stream(*args, **kwargs):
            yield "cloud-ok"

        with patch.dict("os.environ", {"JARVIS_CLOUD_TOKENS_PER_HOUR": "1000"}), \
             patch.object(usage_tracker, "entries", return_value=rows), \
             patch.object(model_router, "_current_mode", "auto"), \
             patch.object(model_router, "_is_mobile_web_active", return_value=False), \
             patch.object(model_router, "forced_model_status", return_value={"active": False}), \
             patch.object(model_router, "_has_local", return_value=True), \
             patch.object(model_router, "_best_local", return_value="jarvis-local"), \
             patch.object(model_router.provider_router, "build_plan", return_value=plan), \
             patch.object(model_router, "ask_stream", side_effect=fake_cloud_stream):
            stream, label = model_router.smart_stream("hello there friend", tool="chat")
            output = "".join(stream)

        self.assertIn("cloud-ok", output)


class SemaphoreClampTests(unittest.TestCase):
    """JARVIS_MAX_CONCURRENT_TASKS hardening: 0 wedges the semaphore (silent
    DoS) and garbage must not crash module import."""

    def test_clamps_and_defaults(self):
        import task_runtime
        cases = {"4": 4, "0": 1, "-3": 1, "100": 32, "banana": 1, None: 1}
        for raw, expected in cases.items():
            self.assertEqual(task_runtime._parse_max_concurrent(raw), expected, raw)


if __name__ == "__main__":
    unittest.main()
