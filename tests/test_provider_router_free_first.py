import unittest
from unittest.mock import patch

import provider_router
import model_router


class ProviderRouterFreeFirstTests(unittest.TestCase):
    def test_auto_mode_prefers_local_candidate_first(self):
        plan = provider_router.build_plan(
            mode="auto",
            tier="mini",
            local_available=True,
            local_model="jarvis-local",
            explicit_cloud=False,
        )
        self.assertGreaterEqual(len(plan.candidates), 1)
        self.assertEqual(plan.candidates[0].provider, "ollama")
        self.assertEqual(plan.candidates[0].model, "jarvis-local")

    def test_mini_tier_prefers_apple_foundation_when_available(self):
        plan = provider_router.build_plan(
            mode="open-source",
            tier="mini",
            local_available=True,
            local_model="jarvis-local",
            apple_foundation_available=True,
            explicit_cloud=False,
        )

        self.assertGreaterEqual(len(plan.candidates), 2)
        self.assertEqual(plan.candidates[0].provider, "apple_foundation")
        self.assertEqual(plan.candidates[0].model, "apple-foundationmodel")
        self.assertEqual(plan.candidates[1].provider, "ollama")

    def test_open_source_can_use_apple_foundation_without_ollama_for_mini(self):
        plan = provider_router.build_plan(
            mode="open-source",
            tier="mini",
            local_available=False,
            local_model="",
            apple_foundation_available=True,
            explicit_cloud=False,
        )

        self.assertEqual(len(plan.candidates), 1)
        self.assertEqual(plan.candidates[0].provider, "apple_foundation")

    def test_non_mini_tier_keeps_ollama_ahead_of_apple_foundation(self):
        plan = provider_router.build_plan(
            mode="open-source",
            tier="sonnet",
            local_available=True,
            local_model="jarvis-local",
            apple_foundation_available=True,
            explicit_cloud=False,
        )

        self.assertGreaterEqual(len(plan.candidates), 1)
        self.assertEqual(plan.candidates[0].provider, "ollama")

    def test_apple_foundation_is_skipped_for_explicit_cloud(self):
        plan = provider_router.build_plan(
            mode="auto",
            tier="mini",
            local_available=True,
            local_model="jarvis-local",
            apple_foundation_available=True,
            explicit_cloud=True,
        )

        self.assertGreaterEqual(len(plan.candidates), 1)
        self.assertNotEqual(plan.candidates[0].provider, "apple_foundation")

    def test_explicit_cloud_need_skips_local_first(self):
        plan = provider_router.build_plan(
            mode="auto",
            tier="sonnet",
            local_available=True,
            local_model="jarvis-local",
            explicit_cloud=True,
        )
        self.assertGreaterEqual(len(plan.candidates), 1)
        self.assertNotEqual(plan.candidates[0].provider, "ollama")

    def test_model_router_falls_back_to_paid_when_local_fails(self):
        previous = model_router.get_mode()
        try:
            model_router.set_mode("auto")

            def broken_local(*_args, **_kwargs):
                def _gen():
                    raise RuntimeError("local unavailable")
                    yield ""
                return _gen()

            with patch("provider_router.PAID_FALLBACK_ENABLED", True), \
                 patch("provider_router.LOCAL_STRICT_FIRST", True), \
                 patch("model_router._has_local", return_value=True), \
                 patch("model_router._best_local", return_value="jarvis-local"), \
                 patch("model_router.ask_local_stream", side_effect=broken_local), \
                 patch("model_router.ask_stream", return_value=iter(["cloud fallback answer"])):
                stream, label = model_router.smart_stream("Say hello.", tool="chat")
                text = "".join(stream)
        finally:
            model_router.set_mode(previous)

        self.assertEqual(label, "Local")
        self.assertIn("cloud fallback answer", text)


if __name__ == "__main__":
    unittest.main()
