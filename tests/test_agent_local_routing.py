"""prefer_local must keep agent-task prompts on the local model.

Agent task prompts embed runtime scaffolding (debrief contract, agent
context) full of words like "analyze"/"review" that trip the chat-tuned
complexity heuristics into the sonnet tier, which routes cloud even when
Ollama is up. task_runtime passes prefer_local=True to opt out.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import model_router
import provider_router

# Scaffolding-heavy prompt that classifies into the sonnet tier.
AGENT_PROMPT = (
    "You are agent backend-engineer. Analyze the task, review the codebase, "
    "and write a comprehensive debrief of what you executed and verified."
)


class PreferLocalRoutingTests(unittest.TestCase):
    def _plan_for(self, prompt: str, prefer_local: bool, local_available: bool = True):
        captured = {}
        real_build_plan = provider_router.build_plan

        def spy_build_plan(**kwargs):
            captured.update(kwargs)
            return real_build_plan(**kwargs)

        with patch.object(model_router, "_current_mode", "auto"), \
             patch.object(model_router, "_is_mobile_web_active", return_value=False), \
             patch.object(model_router, "forced_model_status", return_value={"active": False}), \
             patch.object(model_router, "_has_local", return_value=local_available), \
             patch.object(model_router, "_best_local", return_value="jarvis-local"), \
             patch.object(model_router.provider_router, "build_plan", spy_build_plan):
            # Stream is a lazy generator — never iterated, so no model is called.
            model_router.smart_stream(prompt, tool="chat", prefer_local=prefer_local)
        return captured

    def test_prefer_local_routes_agent_prompt_to_local_tier(self):
        kwargs = self._plan_for(AGENT_PROMPT, prefer_local=True)
        self.assertEqual(kwargs["tier"], "local")
        self.assertFalse(kwargs["explicit_cloud"])
        plan = provider_router.build_plan(**kwargs)
        self.assertTrue(plan.candidates[0].local)

    def test_without_prefer_local_scaffolding_still_escalates(self):
        # Control: proves the heuristic the fix bypasses actually fires.
        # Patch FREE_FIRST_ENABLED=False so the routing policy is cloud-first
        # (no local-preference policy active), isolating the tier escalation behavior.
        kwargs = self._plan_for(AGENT_PROMPT, prefer_local=False)
        self.assertEqual(kwargs["tier"], "sonnet")
        with patch.object(provider_router, "FREE_FIRST_ENABLED", False):
            plan = provider_router.build_plan(**kwargs)
        self.assertFalse(plan.candidates[0].local)

    def test_prefer_local_falls_back_to_normal_routing_when_local_down(self):
        kwargs = self._plan_for(AGENT_PROMPT, prefer_local=True, local_available=False)
        self.assertEqual(kwargs["tier"], "sonnet")
        self.assertFalse(kwargs["local_available"])


if __name__ == "__main__":
    unittest.main()
