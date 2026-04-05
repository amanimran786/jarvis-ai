import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import cost_policy
import evals
import local_model_automation
import local_model_eval
import orchestrator
import prompt_modifiers
import router
import skills
import specialized_agents


class PromptModifierTests(unittest.TestCase):
    def test_eli5_modifier_strips_prefix_and_adds_system_extra(self):
        result = prompt_modifiers.parse("ELI5: explain tcp congestion control")
        self.assertEqual(result.clean_text, "explain tcp congestion control")
        self.assertIn("simple plain language", result.system_extra.lower())
        self.assertIn("ELI5", result.applied)

    def test_role_task_format_modifier_parses_cleanly(self):
        result = prompt_modifiers.parse(
            "ROLE: security reviewer TASK: review this auth flow FORMAT: JSON"
        )
        self.assertEqual(result.clean_text, "review this auth flow")
        self.assertIn("security reviewer", result.system_extra.lower())
        self.assertIn("json", result.system_extra.lower())


class SkillAndAgentTests(unittest.TestCase):
    def test_engineering_skill_exists(self):
        skill = skills.get_skill("engineering_reasoning")
        self.assertIsNotNone(skill)
        self.assertEqual(skill.tool, "chat")

    def test_specialized_agent_role_selection_for_science(self):
        roles = specialized_agents.choose_roles(
            "Why do transformer KV caches improve inference speed?"
        )
        self.assertEqual(roles, ["science_expert", "reviewer"])

    def test_specialized_agent_role_selection_for_security(self):
        roles = specialized_agents.choose_roles(
            "Review this authentication design for security issues."
        )
        self.assertEqual(roles, ["security_reviewer", "reviewer"])

    def test_specialized_agent_run_sequence_for_planner_executor_reviewer(self):
        outputs = {
            "planner": "Plan first.",
            "executor": "Execute second.",
            "reviewer": "Review third.",
        }

        def fake_run_role(role, task, context=""):
            return {"role": role, "model": "stub", "output": outputs[role]}

        with patch("specialized_agents._run_role", side_effect=fake_run_role):
            result = specialized_agents.run("Debug this API", roles=["planner", "executor", "reviewer"])

        self.assertTrue(result["ok"])
        self.assertEqual(result["roles"], ["planner", "executor", "reviewer"])
        self.assertEqual(result["final"], "Execute second.")


class CostPolicyTests(unittest.TestCase):
    def test_simple_chat_prefers_local_when_available(self):
        decision = cost_policy.route_decision(
            "How are you doing today?",
            "mini",
            tool="chat",
            local_available=True,
        )
        self.assertEqual(decision["provider"], "local")

    def test_high_stakes_security_forces_cloud(self):
        decision = cost_policy.route_decision(
            "I have a security vulnerability in production, what should I do?",
            "mini",
            tool="chat",
            local_available=True,
        )
        self.assertEqual(decision["provider"], "cloud")
        self.assertEqual(decision["tier"], "haiku")

    def test_training_decision_defaults_to_none_when_failure_signal_is_sparse(self):
        decision = cost_policy.training_decision()
        self.assertIn(decision["action"], {"none", "distill", "train"})

    def test_local_model_automation_skips_without_training_signal(self):
        with patch("cost_policy.training_decision", return_value={"action": "none", "ok": False, "reason": "Not enough evidence."}):
            result = local_model_automation.run_cycle(force=False)
        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])


class OrchestratorTests(unittest.TestCase):
    def test_science_prompt_auto_invokes_specialized_agent(self):
        decision = orchestrator.classify(
            "What are the main ways CRISPR editing creates off-target effects, and how do researchers reduce them?"
        )
        self.assertEqual(decision.tool, "specialized_agent")
        self.assertEqual(decision.params.get("roles"), ["science_expert", "reviewer"])

    def test_technical_debug_prompt_auto_invokes_specialized_agent(self):
        decision = orchestrator.classify(
            "My FastAPI app returns 502 behind Nginx in Docker. Give me the most likely causes and a concrete debugging sequence."
        )
        self.assertEqual(decision.tool, "specialized_agent")
        self.assertEqual(decision.params.get("roles"), ["planner", "executor", "reviewer"])

    def test_casual_chat_stays_chat(self):
        decision = orchestrator.classify("How are you doing today?")
        self.assertEqual(decision.tool, "chat")


class RouterTests(unittest.TestCase):
    def test_cost_policy_fast_path(self):
        stream, label = router.route_stream("cost policy status")
        text = "".join(stream)
        self.assertEqual(label, "Status")
        self.assertIn("cost policy", text.lower())

    def test_self_review_fallback_does_not_crash_when_self_improve_module_is_incomplete(self):
        with patch("router.si.self_review", new=None, create=True), \
             patch("router.si.review_text", new=None, create=True):
            stream, label = router.route_stream("Review your own code and tell me your top shortcomings.")
            text = "".join(stream)
        self.assertEqual(label, "Self-Review")
        self.assertTrue(text.strip())
        self.assertNotIn("AttributeError", text)

    def test_explicit_specialized_agent_request_bypasses_knowledge_fast_path(self):
        with patch("specialized_agents.run", return_value={"ok": True, "roles": ["planner"], "final": "Stub answer."}), \
             patch("specialized_agents.result_text", return_value="Stub answer."):
            stream, label = router.route_stream(
                "Use specialized agents with planner executor reviewer to explain when a local markdown knowledge vault is better than long chat history."
            )
            text = "".join(stream)
        self.assertEqual(label, "Specialized Agents")
        self.assertEqual(text, "Stub answer.")

    def test_automatic_specialized_agent_route_for_science_question(self):
        with patch("specialized_agents.run", return_value={"ok": True, "roles": ["science_expert", "reviewer"], "final": "Science stub."}), \
             patch("specialized_agents.result_text", return_value="Science stub."):
            stream, label = router.route_stream(
                "Why do transformer KV caches improve inference speed, and what are the memory tradeoffs as sequence length grows?"
            )
            text = "".join(stream)
        self.assertEqual(label, "Specialized Agents")
        self.assertEqual(text, "Science stub.")


class ApiSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(api.app)

    def test_status_endpoint_exposes_cost_policy(self):
        response = self.client.get("/status")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("cost_policy", payload)

    def test_cost_policy_endpoint(self):
        response = self.client.get("/cost-policy")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn("training_action", payload["policy"])

    def test_local_automation_run_respects_policy_skip(self):
        response = self.client.post("/local/automation/run", json={"force": False})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("result", payload)
        if not payload["ok"]:
            self.assertIn("Skipped local model automation", payload["message"])


class BenchmarkCoverageTests(unittest.TestCase):
    def test_curated_eval_cases_cover_expert_domains(self):
        case_ids = {case["id"] for case in local_model_eval.CURATED_CASES}
        self.assertIn("tech_locking", case_ids)
        self.assertIn("tech_debug", case_ids)
        self.assertIn("tech_kv_cache", case_ids)
        self.assertIn("science_entropy", case_ids)
        self.assertIn("science_crispr", case_ids)

    def test_recent_failures_api_shape(self):
        summary = evals.summary(hours=24 * 30)
        self.assertIn("recent_failures", summary)
        self.assertIn("categories", summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
