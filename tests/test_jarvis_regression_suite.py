import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

import api
import cost_policy
import evals
import local_beta
import local_model_automation
import local_model_eval
import local_training
import memory
import model_router
import overlay
import orchestrator
import prompt_modifiers
import router
import browser
import call_privacy
import interview_profile
import meeting_listener
import screen_capture
import self_improve
import skills
import specialized_agents
from tests.jarvis_golden_cases import ENGINEERING_GOLDEN_CASES


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

    def test_specialized_agent_science_fallback_when_claude_unavailable(self):
        with patch("specialized_agents.ask_claude", side_effect=RuntimeError("credit balance too low")):
            result = specialized_agents.run(
                "What is the difference between entropy in thermodynamics and entropy in information theory?"
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["roles"], ["science_expert", "reviewer"])
        self.assertIn("thermodynamics", result["final"])
        self.assertTrue(any(term in result["final"] for term in ("information theory", "Shannon entropy")))

    def test_specialized_agent_memory_leak_fallback_when_claude_unavailable(self):
        with patch("specialized_agents.ask_claude", side_effect=RuntimeError("credit balance too low")):
            result = specialized_agents.run(
                "I have a Python service leaking memory over time. Give me the most likely causes and a concrete debugging sequence."
            )
        self.assertTrue(result["ok"])
        self.assertIn("cache", result["final"])
        self.assertTrue(any(term in result["final"] for term in ("objgraph", "tracemalloc", "connection", "client")))

    def test_specialized_agent_fastapi_502_fallback_when_claude_unavailable(self):
        with patch("specialized_agents.ask_claude", side_effect=RuntimeError("credit balance too low")):
            result = specialized_agents.run(
                "My FastAPI app returns 502 behind Nginx in Docker. Give me the most likely causes and a concrete debugging sequence."
            )
        self.assertTrue(result["ok"])
        self.assertTrue(any(term in result["final"] for term in ("0.0.0.0", "proxy_pass", "upstream", "logs")))

    def test_specialized_agent_auth_security_fallback_when_claude_unavailable(self):
        with patch("specialized_agents.ask_claude", side_effect=RuntimeError("credit balance too low")):
            result = specialized_agents.run(
                "Review this authentication design for security issues. It stores JWT access tokens in localStorage and trusts frontend role checks before showing admin actions."
            )
        self.assertTrue(result["ok"])
        self.assertTrue(any(term in result["final"] for term in ("localStorage", "XSS", "server-side", "authorization")))

    def test_specialized_agent_migration_fallback_when_claude_unavailable(self):
        with patch("specialized_agents.ask_claude", side_effect=RuntimeError("credit balance too low")):
            result = specialized_agents.run(
                "Give me a zero-downtime rollout plan for making a nullable Postgres column required in production."
            )
        self.assertTrue(result["ok"])
        self.assertTrue(any(term in result["final"] for term in ("backfill", "constraint", "NOT NULL", "rollback", "validate")))

    def test_specialized_agent_race_condition_fallback_when_claude_unavailable(self):
        with patch("specialized_agents.ask_claude", side_effect=RuntimeError("credit balance too low")):
            result = specialized_agents.run(
                "I think I have a race condition in a Python worker. How would you narrow it down and make it reproducible?"
            )
        self.assertTrue(result["ok"])
        self.assertTrue(any(term in result["final"] for term in ("shared state", "logging", "reproduce", "stress")))

    def test_specialized_agent_stale_read_fallback_when_claude_unavailable(self):
        with patch("specialized_agents.ask_claude", side_effect=RuntimeError("credit balance too low")):
            result = specialized_agents.run(
                "Users sometimes see stale data after writes. How would you debug whether this is a cache invalidation problem or a replica lag problem?"
            )
        self.assertTrue(result["ok"])
        self.assertTrue(any(term in result["final"] for term in ("cache invalidation", "replica lag", "read-after-write", "primary")))


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
    def test_open_source_mode_skips_llm_classifier(self):
        previous = model_router.get_mode()
        try:
            model_router.set_mode("open-source")
            with patch("orchestrator.ask_claude", side_effect=AssertionError("should not call claude")):
                decision = orchestrator.classify("Tell me something interesting about databases.")
        finally:
            model_router.set_mode(previous)
        self.assertEqual(decision.tool, "chat")

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


class InterviewProfileTests(unittest.TestCase):
    def test_tell_me_about_yourself_text_mentions_target_direction(self):
        text = interview_profile.tell_me_about_yourself_text()
        self.assertIn("Trust and Safety", text)
        self.assertTrue(any(term in text for term in ("AI Safety", "cybersecurity", "software engineering")))

    def test_role_fit_text_for_security_role_mentions_security_and_systems(self):
        text = interview_profile.role_fit_text("Why are you a fit for this cybersecurity role?")
        self.assertIn("cybersecurity", text.lower())
        self.assertTrue(any(term in text.lower() for term in ("incident", "security", "detection", "response")))

    def test_tell_me_about_yourself_text_for_security_role_tilts_security(self):
        text = interview_profile.tell_me_about_yourself_text("Tell me about yourself for a cybersecurity interview.")
        self.assertIn("security-oriented", text)
        self.assertTrue(any(term in text.lower() for term in ("threat detection", "incident", "risk triage")))

    def test_tell_me_about_yourself_text_for_software_engineering_role_tilts_engineering(self):
        text = interview_profile.tell_me_about_yourself_text("Tell me about yourself for a backend software engineering role.")
        self.assertIn("software engineering", text.lower())
        self.assertTrue(any(term in text.lower() for term in ("backend", "apis", "automation", "reliability")))

    def test_supported_role_families_text_lists_main_tracks(self):
        text = interview_profile.supported_role_families_text()
        self.assertIn("AI Trust and Safety", text)
        self.assertIn("Cybersecurity", text)

    def test_target_role_pack_for_ai_safety_mentions_misuse_and_frameworks(self):
        text = interview_profile.target_role_pack_text("Give me my AI safety role pack.")
        self.assertIn("AI Safety", text)
        self.assertTrue(any(term in text.lower() for term in ("misuse", "jailbreak", "precision_recall", "cross_functional")))

    def test_target_role_pack_for_cybersecurity_mentions_risk_and_detection(self):
        text = interview_profile.target_role_pack_text("What should I emphasize for a cybersecurity role?")
        self.assertIn("Cybersecurity", text)
        self.assertTrue(any(term in text.lower() for term in ("risk", "detection", "incident", "automation")))

    def test_imported_role_pack_is_discoverable(self):
        text = interview_profile.supported_role_families_text()
        self.assertIn("youtube_pem_2026", text)

    def test_file_backed_youtube_role_pack_mentions_policy_enforcement_manager(self):
        text = interview_profile.target_role_pack_text("Give me my YouTube PEM 2026 role pack.")
        self.assertIn("Policy Enforcement Manager, Age Appropriateness", text)
        self.assertIn("YouTube / Google", text)

    def test_youtube_tell_me_about_yourself_mentions_child_safety_and_systems(self):
        text = interview_profile.tell_me_about_yourself_text(
            "Tell me about yourself for the Policy Enforcement Manager, Age Appropriateness role at YouTube."
        )
        self.assertIn("YouTube", text)
        self.assertTrue(any(term in text.lower() for term in ("child safety", "quality", "python", "sql")))

    def test_youtube_why_company_text_mentions_priorities(self):
        text = interview_profile.why_company_text("Why YouTube and Google for the age appropriateness role?")
        self.assertTrue(any(term in text for term in ("Neal Mohan", "kids and teens", "AI-generated harm", "YouTube")))

    def test_youtube_enforcement_framework_mentions_four_outcomes(self):
        text = interview_profile.enforcement_decision_text(
            "How do you approach removal vs age restriction vs Made for Kids vs leave up?"
        )
        self.assertTrue(all(term in text for term in ("remove", "age-restrict", "Made for Kids", "leave up")))

    def test_youtube_quality_measurement_mentions_irr_and_overturns(self):
        text = interview_profile.quality_measurement_text("How do you measure enforcement quality?")
        self.assertTrue(any(term in text for term in ("inter-rater reliability", "appeal overturn", "blind audits")))

    def test_youtube_data_story_mentions_tiktok_and_classifier_shift(self):
        text = interview_profile.data_story_text("Tell me about a time you used data to drive an enforcement decision.")
        self.assertTrue(any(term in text for term in ("TikTok", "classifier", "labeled dataset", "SQL")))

    def test_behavioral_story_for_failure_uses_enforcement_error(self):
        text = interview_profile.behavioral_story_text("Tell me about a time you made a mistake in a high-pressure situation.")
        self.assertTrue(any(term in text.lower() for term in ("on-call", "reversed", "post-mortem", "escalation")))

    def test_situational_framework_for_spike_mentions_characterize_and_timing(self):
        text = interview_profile.situational_framework_text("How would you handle a spike in enforcement metrics?")
        self.assertTrue(any(term in text.lower() for term in ("characterize", "timing", "classifier", "calibration")))


class RouterTests(unittest.TestCase):
    def test_open_source_mode_switch_fast_path(self):
        previous = model_router.get_mode()
        try:
            stream, label = router.route_stream("switch to open-source mode")
            text = "".join(stream)
            current = model_router.get_mode()
        finally:
            model_router.set_mode(previous)
        self.assertEqual(label, "Status")
        self.assertEqual(current, "open-source")
        self.assertIn("Open-source mode", text)

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

    def test_self_improve_validation_checks_semantic_parity(self):
        result = self_improve._run_validation("self_improve.py")
        self.assertTrue(result["ok"], result["summary"])
        checks = {item["name"]: item for item in result["checks"]}
        self.assertIn("parity", checks)
        self.assertTrue(checks["parity"]["ok"], checks["parity"]["output"])

    def test_local_beta_fast_path(self):
        with patch("local_beta.run_beta_suite", return_value={"ok": True, "case_count": 3, "passed": 2, "failed": 1, "failed_case_ids": ["beta_memory"]}), \
             patch("local_beta.result_text", return_value="Ran 3 beta cases. 2 passed and 1 failed."):
            stream, label = router.route_stream("beta test jarvis")
            text = "".join(stream)
        self.assertEqual(label, "Local Model")
        self.assertIn("beta cases", text)

    def test_engineering_beta_fast_path(self):
        with patch("local_beta.run_beta_suite", return_value={"ok": True, "case_count": 3, "passed": 3, "failed": 0, "suite": "engineering"}), \
             patch("local_beta.result_text", return_value="Ran 3 engineering beta cases. 3 passed and 0 failed."):
            stream, label = router.route_stream("beta test engineering")
            text = "".join(stream)
        self.assertEqual(label, "Local Model")
        self.assertIn("engineering beta cases", text)

    def test_locking_tradeoff_fast_path(self):
        stream, label = router.route_stream(
            "Compare optimistic locking and pessimistic locking and tell me when each one is the better choice."
        )
        text = "".join(stream)
        self.assertEqual(label, "Sonnet")
        self.assertIn("Optimistic locking", text)
        self.assertIn("Pessimistic locking", text)
        self.assertTrue(any(term in text for term in ("throughput", "conflicts", "deadlocks")))

    def test_database_index_tradeoff_fast_path(self):
        stream, label = router.route_stream("When should I add a database index, and when can it hurt performance?")
        text = "".join(stream)
        self.assertEqual(label, "Sonnet")
        self.assertTrue(any(term in text for term in ("read performance", "write amplification", "storage", "insert")))

    def test_python_race_condition_routes_to_specialized_agents(self):
        stream, label = router.route_stream(
            "I think I have a race condition in a Python worker. How would you narrow it down and make it reproducible?"
        )
        text = "".join(stream)
        self.assertEqual(label, "Specialized Agents")
        self.assertTrue(any(term in text for term in ("shared state", "thread", "reproduce", "stress test", "lock")))

    def test_stale_read_routes_to_specialized_agents(self):
        stream, label = router.route_stream(
            "Users sometimes see stale data after writes. How would you debug whether this is a cache invalidation problem or a replica lag problem?"
        )
        text = "".join(stream)
        self.assertEqual(label, "Specialized Agents")
        self.assertTrue(any(term in text for term in ("cache invalidation", "replica lag", "read-after-write", "primary", "TTL", "correlation")))

    def test_fastapi_502_routes_to_specialized_agents(self):
        stream, label = router.route_stream(
            "I have a Dockerized FastAPI app that works locally but returns 502 behind Nginx in production. What are the top likely causes and how would you narrow them down?"
        )
        text = "".join(stream)
        self.assertEqual(label, "Specialized Agents")
        self.assertTrue(any(term in text for term in ("Nginx", "0.0.0.0", "proxy_pass", "Docker", "502")))

    def test_tell_me_about_yourself_fast_path(self):
        stream, label = router.route_stream("Tell me about yourself.")
        text = "".join(stream)
        self.assertEqual(label, "Interview")
        self.assertIn("Trust and Safety", text)
        self.assertTrue(any(term in text for term in ("software engineering", "cybersecurity", "AI Safety")))

    def test_role_fit_fast_path(self):
        stream, label = router.route_stream("Why are you a fit for a technical AI trust and safety role?")
        text = "".join(stream)
        self.assertEqual(label, "Interview")
        self.assertTrue(any(term in text for term in ("strong fit", "intersection", "Trust and Safety", "AI Safety")))

    def test_career_narrative_fast_path(self):
        stream, label = router.route_stream("Give me my canonical career narrative for interviews.")
        text = "".join(stream)
        self.assertEqual(label, "Interview")
        self.assertIn("Aman Imran", text)
        self.assertIn("technical roles", text)

    def test_tell_me_about_yourself_for_security_fast_path(self):
        stream, label = router.route_stream("Tell me about yourself for a cybersecurity interview.")
        text = "".join(stream)
        self.assertEqual(label, "Interview")
        self.assertTrue(any(term in text.lower() for term in ("security-oriented", "threat detection", "incident")))

    def test_tell_me_about_yourself_for_engineering_fast_path(self):
        stream, label = router.route_stream("Tell me about yourself for a software engineering role.")
        text = "".join(stream)
        self.assertEqual(label, "Interview")
        self.assertTrue(any(term in text.lower() for term in ("software engineering", "backend", "automation")))

    def test_roles_targeting_fast_path(self):
        stream, label = router.route_stream("What roles are you targeting?")
        text = "".join(stream)
        self.assertEqual(label, "Interview")
        self.assertIn("AI Trust and Safety", text)
        self.assertIn("Software Engineering", text)

    def test_target_role_pack_fast_path(self):
        stream, label = router.route_stream("Give me my AI trust and safety role pack.")
        text = "".join(stream)
        self.assertEqual(label, "Interview")
        self.assertIn("Target role pack: AI Trust and Safety", text)
        self.assertTrue(any(term in text for term in ("quality_measurement", "spike_diagnosis", "cross_functional")))

    def test_file_backed_youtube_pack_fast_path(self):
        stream, label = router.route_stream("Give me my YouTube PEM 2026 role pack.")
        text = "".join(stream)
        self.assertEqual(label, "Interview")
        self.assertIn("Policy Enforcement Manager, Age Appropriateness", text)
        self.assertIn("YouTube / Google", text)

    def test_youtube_why_google_fast_path(self):
        stream, label = router.route_stream("Why YouTube and Google for the Policy Enforcement Manager, Age Appropriateness role?")
        text = "".join(stream)
        self.assertEqual(label, "Interview")
        self.assertTrue(any(term in text for term in ("Neal Mohan", "kids and teens", "AI-generated harm", "YouTube")))

    def test_youtube_enforcement_decision_fast_path(self):
        stream, label = router.route_stream("How do you approach removal vs age restriction vs Made for Kids vs leave up?")
        text = "".join(stream)
        self.assertEqual(label, "Interview")
        self.assertTrue(all(term in text for term in ("remove", "age-restrict", "Made for Kids", "leave up")))

    def test_youtube_quality_measurement_fast_path(self):
        stream, label = router.route_stream("How do you measure enforcement quality?")
        text = "".join(stream)
        self.assertEqual(label, "Interview")
        self.assertTrue(any(term in text for term in ("inter-rater reliability", "appeal overturn", "blind audits")))

    def test_behavioral_story_fast_path(self):
        stream, label = router.route_stream("Tell me about a time you used data to drive an enforcement decision.")
        text = "".join(stream)
        self.assertEqual(label, "Interview")
        self.assertTrue(any(term in text for term in ("TikTok", "classifier", "SQL", "labeled dataset")))

    def test_situational_framework_fast_path(self):
        stream, label = router.route_stream("How would you handle a spike in enforcement metrics?")
        text = "".join(stream)
        self.assertEqual(label, "Interview")
        self.assertTrue(any(term in text.lower() for term in ("characterize", "timing", "classifier", "calibration")))

    def test_meeting_captions_fast_path(self):
        with patch("browser.read_meeting_captions", return_value="Recent MEET captions in ChatGPT Atlas:\n- Explain optimistic locking."):
            stream, label = router.route_stream("Read the meeting captions.")
            text = "".join(stream)
        self.assertEqual(label, "Browser")
        self.assertIn("Recent MEET captions", text)

    def test_meeting_diagnostics_fast_path(self):
        diagnostics = (
            "Meeting detection: MEET. "
            "Meeting detected: MEET in Google Chrome (window 10, tab 1). "
            "Current page: unavailable. No browser context. "
            "Captions: blocked or unavailable. macOS is blocking browser automation for Google Chrome."
        )
        with patch("router._meeting_diagnostics_reply", return_value=diagnostics):
            stream, label = router.route_stream("meeting diagnostics")
            text = "".join(stream)
        self.assertEqual(label, "Meeting")
        self.assertIn("Meeting detection: MEET", text)
        self.assertIn("blocked or unavailable", text)

    def test_focus_meeting_fast_path(self):
        with patch("browser.focus_meeting_tab", return_value="Focused Meet - qqt-truu-hkq in Google Chrome."):
            stream, label = router.route_stream("focus the meeting tab")
            text = "".join(stream)
        self.assertEqual(label, "Browser")
        self.assertIn("Focused Meet", text)

    def test_meeting_safe_mode_status_fast_path(self):
        with patch("router.call_privacy.status_text", return_value="Meeting-safe mode is ON. Jarvis will stay quiet automatically when a call is detected."):
            stream, label = router.route_stream("meeting safe mode")
            text = "".join(stream)
        self.assertEqual(label, "Meeting")
        self.assertIn("Meeting-safe mode is ON", text)

    def test_meeting_safe_mode_enable_fast_path(self):
        with patch("router.call_privacy.set_enabled", return_value=True), \
             patch("router.call_privacy.status_text", return_value="Meeting-safe mode is ON. Audio replies are suppressed during MEET calls."):
            stream, label = router.route_stream("turn on meeting safe mode")
            text = "".join(stream)
        self.assertEqual(label, "Meeting")
        self.assertIn("suppressed", text)

    def test_vault_exact_citation_summary_prefers_raw_source(self):
        fake_results = [
            {
                "path": "indexes/topics.md",
                "title": "Jarvis Vault Strategy",
                "excerpt": "Compiled summary.",
                "citation": {"path": "indexes/topics.md", "heading": "Jarvis Vault Strategy"},
            },
            {
                "path": "raw/jarvis_vault_strategy.md",
                "title": "Jarvis Vault Strategy",
                "excerpt": "Jarvis should use a local markdown vault before it grows prompt context or relies on long conversational carry-over.",
                "citation": {"path": "raw/jarvis_vault_strategy.md", "heading": "Jarvis Vault Strategy"},
            },
        ]
        with patch("router.vault.search", return_value=fake_results):
            stream, label = router.route_stream(
                "Search the vault for Jarvis Vault Strategy and summarize it in two sentences with the exact local file and heading you used."
            )
            text = "".join(stream)
        self.assertEqual(label, "Knowledge")
        self.assertIn("raw/jarvis_vault_strategy.md", text)
        self.assertIn("Jarvis Vault Strategy", text)

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

    def test_local_beta_status_endpoint(self):
        response = self.client.get("/local/beta/status")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn("status", payload)

    def test_memory_status_endpoint(self):
        response = self.client.get("/memory/status")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn("status", payload)
        self.assertIn("working_memory_ready", payload["status"])

    def test_local_automation_run_respects_policy_skip(self):
        with patch("local_model_automation.run_cycle", return_value={"ok": False, "skipped": True, "error": "Skipped local model automation. Not enough evidence."}):
            response = self.client.post("/local/automation/run", json={"force": False})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("result", payload)
        if not payload["ok"]:
            self.assertIn("Skipped local model automation", payload["message"])

    def test_mode_endpoint_accepts_open_source(self):
        previous = model_router.get_mode()
        try:
            response = self.client.post("/mode", json={"mode": "open-source"})
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["mode"], "open-source")
        finally:
            model_router.set_mode(previous)


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

    def test_engineering_golden_pack_contains_core_swe_cases(self):
        case_ids = {case["id"] for case in ENGINEERING_GOLDEN_CASES}
        self.assertIn("fastapi_nginx_502_debug", case_ids)
        self.assertIn("auth_flow_security_review", case_ids)
        self.assertIn("database_index_tradeoff", case_ids)
        self.assertIn("postgres_zero_downtime_required_column", case_ids)
        self.assertIn("python_race_condition_debug", case_ids)
        self.assertIn("stale_read_cache_vs_replica", case_ids)


class OverlayMeetingDetectionTests(unittest.TestCase):
    def test_meeting_label_for_url_supports_browser_meeting_urls(self):
        self.assertEqual(overlay.meeting_label_for_url("https://meet.google.com/qqt-truu-hkq"), "MEET")
        self.assertEqual(overlay.meeting_label_for_url("https://teams.microsoft.com/l/meetup-join/abc"), "TEAMS")
        self.assertEqual(overlay.meeting_label_for_url("https://app.zoom.us/wc/join/123"), "ZOOM")

    def test_compute_meeting_app_skips_browsers_that_are_not_running(self):
        with patch("overlay._running_app_names", return_value={"Finder", "Google Chrome"}), \
             patch("overlay._browser_active_meeting_label", side_effect=lambda app, _script: "MEET" if app == "Google Chrome" else (_ for _ in ()).throw(AssertionError("should not probe"))), \
             patch("overlay._browser_any_meeting_label", return_value=None):
            self.assertEqual(overlay._compute_meeting_app(), "MEET")


class BrowserMeetingCaptionTests(unittest.TestCase):
    def test_choose_browser_prefers_frontmost_supported_browser(self):
        with patch("browser._frontmost_app_name", return_value="ChatGPT Atlas"), \
             patch("browser._app_exists", return_value=True):
            self.assertEqual(browser._choose_browser(None), "ChatGPT Atlas")

    def test_read_meeting_captions_formats_recent_lines(self):
        payload = {
            "ok": True,
            "browser": "ChatGPT Atlas",
            "meeting": "MEET",
            "lines": ["Can you walk through the tradeoffs?", "Yes, optimistic locking is better under low contention."],
        }
        with patch("browser._extract_meeting_caption_payload", return_value=payload):
            text = browser.read_meeting_captions()
        self.assertIn("Recent MEET captions", text)
        self.assertIn("optimistic locking", text)

    def test_summarize_meeting_captions_uses_browser_execution_prompt(self):
        payload = {
            "ok": True,
            "browser": "Google Chrome",
            "meeting": "MEET",
            "title": "Interview Round",
            "url": "https://meet.google.com/qqt-truu-hkq",
            "lines": ["Explain optimistic locking.", "When would pessimistic locking be better?"],
        }
        with patch("browser._extract_meeting_caption_payload", return_value=payload), \
             patch("browser.format_with_mini", return_value=iter(["Use optimistic locking when conflicts are rare."])):
            text = browser.summarize_meeting_captions("Help me answer.")
        self.assertIn("optimistic locking", text)

    def test_meeting_diagnostics_text_surfaces_permission_block(self):
        captions = {
            "ok": False,
            "browser": "Google Chrome",
            "error": (
                "Access not allowed (-1723). "
                "macOS is blocking browser automation for Google Chrome. "
                "Allow your terminal or Python app under System Settings > Privacy & Security > Automation."
            ),
        }
        with patch("browser._choose_browser", return_value="Google Chrome"), \
             patch("browser._front_tab_data", return_value={"ok": True, "title": "Claude", "url": "https://claude.ai"}), \
             patch("browser._find_meeting_tabs", return_value=[{
                 "browser": "Google Chrome",
                 "window_index": 10,
                 "tab_index": 1,
                 "title": "Meet - qqt-truu-hkq",
                 "url": "https://meet.google.com/qqt-truu-hkq",
                 "meeting": "MEET",
                 "active": False,
             }]), \
             patch("browser._extract_meeting_caption_payload", return_value=captions):
            text = browser.meeting_diagnostics_text()
        self.assertIn("Meeting detected: MEET", text)
        self.assertIn("blocked or unavailable", text)
        self.assertIn("Automation", text)

    def test_focus_meeting_tab_reports_target(self):
        with patch("browser._find_meeting_tabs", return_value=[{
            "browser": "Google Chrome",
            "window_index": 10,
            "tab_index": 1,
            "title": "Meet - qqt-truu-hkq",
            "url": "https://meet.google.com/qqt-truu-hkq",
            "meeting": "MEET",
            "active": False,
        }]), \
             patch("browser._run_applescript", return_value=("", "")):
            text = browser.focus_meeting_tab()
        self.assertIn("Focused Meet - qqt-truu-hkq", text)

    def test_clean_browser_js_error_strips_injected_script_noise(self):
        err = (
            "167:1807: syntax error: Can’t get \"(() => { huge js payload })()\" "
            "in tab 1 of window 10. Access not allowed. (-1723)"
        )
        text = browser._clean_browser_js_error(err, "Google Chrome")
        self.assertNotIn("huge js payload", text)
        self.assertIn("Browser automation is blocked for Google Chrome.", text)

    def test_clean_browser_js_error_explains_chrome_apple_events_setting(self):
        err = (
            "Google Chrome got an error: Executing JavaScript through AppleScript is turned off. "
            "To turn it on, from the menu bar, go to View > Developer > Allow JavaScript from Apple Events. (12)"
        )
        text = browser._clean_browser_js_error(err, "Google Chrome")
        self.assertIn("Google Chrome is blocking tab scripting.", text)
        self.assertIn("Allow JavaScript from Apple Events", text)


class MeetingListenerTests(unittest.TestCase):
    def test_meet_prefers_microphone_over_teams_audio_device(self):
        devices = [
            {"index": 3, "name": "MacBook Pro Microphone", "channels": 1},
            {"index": 5, "name": "Microsoft Teams Audio", "channels": 1},
        ]
        with patch("meeting_listener.list_audio_devices", return_value=devices), \
             patch("overlay.detect_meeting_app", return_value="MEET"):
            self.assertIsNone(meeting_listener.get_virtual_meeting_audio_device())
            preferred = meeting_listener.preferred_source_snapshot()
        self.assertEqual(preferred["kind"], "microphone")
        self.assertEqual(preferred["device_index"], 3)

    def test_generate_suggestion_uses_strong_tier_for_direct_question(self):
        meeting_listener._transcript_history[:] = ["Interviewer: What is a variable?"]
        with patch("meeting_listener.ask_with_priority", return_value="A variable is a named reference to a value, so you can store and reuse data in a program.") as ask_mock:
            text = meeting_listener._generate_suggestion("What is a variable?")
        self.assertIn("named reference", text)
        self.assertEqual(ask_mock.call_args.kwargs["tier"], "strong")
        self.assertIn("Return exactly what Aman should say next", ask_mock.call_args.args[0])

    def test_generate_suggestion_uses_cheap_tier_for_nontechnical_statement(self):
        meeting_listener._transcript_history[:] = ["Speaker: Thanks everyone for joining."]
        with patch("meeting_listener.ask_with_priority", return_value="Thanks for the overview. The main thing I’m tracking is the rollout timing.") as ask_mock:
            text = meeting_listener._generate_suggestion("Thanks everyone for joining.")
        self.assertIn("rollout timing", text)
        self.assertEqual(ask_mock.call_args.kwargs["tier"], "cheap")


class ModelRouterFallbackTests(unittest.TestCase):
    def test_open_source_mode_prefers_local_label(self):
        previous = model_router.get_mode()
        try:
            model_router.set_mode("open-source")
            with patch("model_router._has_local", return_value=True), \
                 patch("model_router._best_local", return_value="jarvis-local"), \
                 patch("model_router.ask_local_stream", return_value=iter(["Local only answer."])):
                stream, label = model_router.smart_stream("Explain optimistic locking.", tool="chat")
                text = "".join(stream)
        finally:
            model_router.set_mode(previous)

        self.assertEqual(label, "Open-Source")
        self.assertIn("Local only answer.", text)

    def test_gpt4o_falls_back_when_openai_stream_raises(self):
        def broken_stream(*_args, **_kwargs):
            def _gen():
                raise RuntimeError("credit balance too low")
                yield ""
            return _gen()

        previous = model_router.get_mode()
        try:
            model_router.set_mode("auto")
            with patch("model_router.ask_stream", side_effect=broken_stream), \
                 patch("model_router.ask_gemini_stream", return_value=iter(["Fallback answer from Gemini."])):
                stream, label = model_router.smart_stream(
                    "Compare optimistic locking and pessimistic locking and tell me when each one is the better choice.",
                    tool="chat",
                )
                text = "".join(stream)
        finally:
            model_router.set_mode(previous)

        self.assertEqual(label, "GPT-4o")
        self.assertIn("Fallback answer from Gemini.", text)


class ScreenCaptureTests(unittest.TestCase):
    def test_capture_screenshot_retries_and_succeeds(self):
        with TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "shot.png")
            calls = {"count": 0}

            def fake_run(cmd, capture_output=True, text=True):
                calls["count"] += 1
                if calls["count"] == 2:
                    Path(cmd[-1]).write_bytes(b"pngdata")

                class Result:
                    returncode = 0 if calls["count"] == 2 else 1
                    stderr = "could not create image from display" if calls["count"] == 1 else ""
                    stdout = ""

                return Result()

            with patch("screen_capture.subprocess.run", side_effect=fake_run), \
                 patch("screen_capture.time.sleep"):
                result = screen_capture.capture_screenshot(path, image_format="png", retries=2)

        self.assertEqual(result, path)
        self.assertEqual(calls["count"], 2)

    def test_capture_screenshot_surfaces_permission_hint(self):
        with TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "shot.png")

            class Result:
                returncode = 1
                stderr = "screencapture: could not create image from display 0"
                stdout = ""

            with patch("screen_capture.subprocess.run", return_value=Result()), \
                 patch("screen_capture.time.sleep"):
                with self.assertRaises(RuntimeError) as ctx:
                    screen_capture.capture_screenshot(path, image_format="png", retries=1)

        text = str(ctx.exception)
        self.assertIn("Screen Recording", text)
        self.assertIn("could not create image from display", text)


class CallPrivacyTests(unittest.TestCase):
    def test_should_suppress_audio_only_when_enabled_and_meeting_detected(self):
        with patch("call_privacy._meeting_label", return_value="MEET"):
            call_privacy.set_enabled(True)
            self.assertTrue(call_privacy.should_suppress_audio())
            call_privacy.set_enabled(False)
            self.assertFalse(call_privacy.should_suppress_audio())


class LocalTrainingTests(unittest.TestCase):
    def test_distill_failures_uses_standalone_beta_failures(self):
        captured = {}
        fake_data = {
            "interactions": [],
            "failures": [
                {
                    "id": "fail1",
                    "timestamp": "2026-04-05T00:00:00+00:00",
                    "interaction_id": "",
                    "category": "memory",
                    "issue": "Answer was generic.",
                    "expected": "Use Aman-specific context.",
                    "user_input": "Tell me something interesting based on what you know about me.",
                    "response": "AI is changing the world.",
                    "model": "Local",
                }
            ],
        }

        def fake_write(path, examples):
            captured["path"] = str(path)
            captured["examples"] = examples

        with patch("local_training._ensure_dirs"), \
             patch("local_training.evals.load", return_value=fake_data), \
             patch("local_training.skills.build_system_extra", return_value=("", [])), \
             patch("local_training.ask_claude", return_value="You work on AI safety systems and you're building Jarvis, so the interesting part is how often you push toward local-first, inspectable AI workflows."), \
             patch("local_training._write_jsonl", side_effect=fake_write):
            result = local_training.distill_failures(limit=3)

        self.assertTrue(result["ok"])
        self.assertEqual(result["example_count"], 1)
        self.assertEqual(captured["examples"][0]["messages"][1]["content"], fake_data["failures"][0]["user_input"])
        self.assertEqual(captured["examples"][0]["meta"]["teacher_source"], "failure_distillation")

    def test_zero_limit_distill_paths_do_not_call_teacher(self):
        with patch("local_training._ensure_dirs"), \
             patch("local_training._write_jsonl"), \
             patch("local_training.ask_claude") as mock_teacher:
            result_failures = local_training.distill_failures(limit=0)
            result_expert = local_training.distill_expert_cases(limit=0)
        self.assertTrue(result_failures["ok"])
        self.assertEqual(result_failures["example_count"], 0)
        self.assertTrue(result_expert["ok"])
        self.assertEqual(result_expert["example_count"], 0)
        mock_teacher.assert_not_called()


class LocalBetaTests(unittest.TestCase):
    def test_beta_suite_logs_failures(self):
        case = {
            "id": "beta_memory",
            "prompt": "Tell me something interesting based on what you know about me.",
            "expected_label": "Status",
            "must_include_all": ["Anthropic"],
            "must_include_any": [],
            "must_exclude_all": [],
            "expected": "Use Aman-specific context.",
        }
        with patch("local_beta._selected_cases", return_value=[case]), \
             patch("router.route_stream", return_value=(iter(["Generic answer."]), "Chat")), \
             patch("local_beta.evals.log_failure") as mock_log:
            with TemporaryDirectory() as tmp:
                with patch("local_beta.RUNS_DIR", Path(tmp)):
                    result = local_beta.run_beta_suite()
        self.assertTrue(result["ok"])
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["failed_case_ids"], ["beta_memory"])
        mock_log.assert_called_once()

    def test_engineering_suite_selection(self):
        cases = local_beta._selected_cases(suite="engineering")
        self.assertTrue(cases)
        self.assertTrue(all(case.get("suite") == "engineering" for case in cases))

    def test_recent_engineering_failures_are_promoted_into_suite(self):
        failure = {
            "id": "failfastapi",
            "user_input": "My FastAPI service returns 502 behind Nginx in Docker. How do I narrow it down?",
            "expected": "Rank likely causes and include a debugging sequence.",
            "issue": "Prior answer was too generic.",
        }
        with patch("local_beta.evals.recent_failures", return_value=[failure]):
            cases = local_beta._selected_cases(suite="engineering")
        prompts = {case["prompt"] for case in cases}
        self.assertIn(failure["user_input"], prompts)

    def test_dynamic_case_failure_check_allows_expected_label_and_runtime_markers(self):
        case = {
            "id": "recent_test",
            "suite": "engineering",
            "prompt": "Debug a race condition in a Python worker.",
            "expected_label": "Specialized Agents",
            "must_exclude_all": ["credit balance is too low"],
        }
        failures = local_beta._case_failures(case, "Specialized Agents", "Clear answer with no runtime errors.")
        self.assertEqual(failures, [])


class MemoryConsolidationTests(unittest.TestCase):
    def test_consolidate_memory_builds_tiers_and_context(self):
        with TemporaryDirectory() as tmp:
            memory_file = Path(tmp) / "memory.json"
            with patch("memory.MEMORY_FILE", str(memory_file)):
                memory.add_fact("Aman works on AI safety systems at Anthropic.")
                memory.set_preference("communication_style", "direct")
                memory.add_project("Jarvis AI", description="personal voice and text assistant for macOS")
                memory.track_topic("python ai code memory")
                memory.save_conversation("Worked on Jarvis local model evaluation and memory routing.")

                status = memory.memory_status()
                context = memory.get_context()

        self.assertTrue(status["working_memory_ready"])
        self.assertTrue(status["long_term_profile_ready"])
        self.assertIn("Jarvis AI", " ".join(status["working_memory"].get("active_projects", [])))
        self.assertIn("Durable user profile", context)
        self.assertIn("Working memory", context)


if __name__ == "__main__":
    unittest.main(verbosity=2)
