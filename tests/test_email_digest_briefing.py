"""
tests/test_email_digest_briefing.py

Targeted tests for:
  - Email digest fast-path detection (various phrasings → correct label)
  - Email digest handler calls google_services and streams summary
  - _agent_calendar_upcoming returns formatted calendar text
  - _agent_pending_alerts returns empty string when no alerts

No LLM calls — everything is mocked.
"""

from __future__ import annotations

import sys
import os
import types
import unittest
from unittest.mock import MagicMock, patch

# Ensure repo root is on path
_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ── Stub helpers (match test_message_intent_parsing.py pattern) ──────────────

def _install_stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


_SAVED_MODULES: dict = {}
_saved_memory_track_topic = None
_saved_pm_parse = None
_saved_desktop_overlay = None


def _setup_router_stubs():
    global _SAVED_MODULES, _saved_memory_track_topic, _saved_pm_parse, _saved_desktop_overlay
    if _SAVED_MODULES:
        return

    # google_services needs to be a MagicMock so tests can set return_value on its methods
    _gs_mock = MagicMock()
    _SAVED_MODULES["google_services"] = sys.modules.get("google_services")
    sys.modules["google_services"] = _gs_mock

    heavy_deps = [
        "speech_recognition", "ollama", "pyaudio", "sounddevice",
        "anthropic", "uvicorn", "sklearn", "transformers",
        "tools", "terminal", "browser", "desktop", "notes",
        "camera", "meeting_listener", "memory", "memory_layer", "evals",
        "skills", "vault", "vault_capture", "source_ingest", "skill_factory",
        "interview_profile", "specialized_agents", "behavior_hooks",
        "capability_evals", "capability_parity", "cost_policy",
        "context_budget", "coder_workbench", "external_agent_patterns",
        "production_readiness", "security_roe", "usage_tracker",
        "prompt_modifiers", "self_improve", "hardware", "runtime_state",
        "messages", "call_privacy", "provider_router", "semantic_memory",
        "model_router",
    ]
    for name in heavy_deps:
        _SAVED_MODULES[name] = sys.modules.get(name)
        if name not in sys.modules:
            if "." in name:
                parent = name.split(".")[0]
                if parent not in sys.modules:
                    _install_stub(parent)
            _install_stub(name)

    # local_runtime submodules
    _SAVED_MODULES["local_runtime"] = sys.modules.get("local_runtime")
    for sub in [
        "local_runtime.local_training", "local_runtime.local_model_eval",
        "local_runtime.local_model_automation", "local_runtime.local_beta",
        "local_runtime.local_model_benchmark", "local_runtime.model_fleet",
    ]:
        _SAVED_MODULES[sub] = sys.modules.get(sub)
        if sub not in sys.modules:
            parent = sub.split(".")[0]
            if parent not in sys.modules:
                pm = _install_stub(parent)
                pm.__path__ = []
            _install_stub(sub)

    # desktop.overlay — save real overlay via _SAVED_MODULES so teardown restores it.
    # Also save the .overlay attribute on the real desktop module (monkey-patched below).
    _saved_desktop_overlay = sys.modules.get("desktop.overlay")
    _SAVED_MODULES.setdefault("desktop.overlay", sys.modules.get("desktop.overlay"))
    if "desktop" not in sys.modules:
        _install_stub("desktop", overlay=_install_stub("desktop.overlay"))
    else:
        sys.modules["desktop"].overlay = _install_stub("desktop.overlay")

    if "config" not in sys.modules:
        _install_stub("config", OPUS="gpt-4o", SONNET="claude-3-5-sonnet-20241022")

    _saved_pm_parse = getattr(sys.modules.get("prompt_modifiers"), "parse", None)
    sys.modules["prompt_modifiers"].parse = lambda text: types.SimpleNamespace(
        clean_text=text, system_extra=""
    )

    _mem = sys.modules.get("memory")
    if _mem is not None:
        _saved_memory_track_topic = getattr(_mem, "track_topic", None)
    sys.modules["memory"].track_topic = lambda *a, **kw: None

    if "model_router" not in sys.modules or not hasattr(sys.modules["model_router"], "smart_stream"):
        _install_stub(
            "model_router",
            smart_stream=lambda *a, **kw: ("", ""),
            format_with_mini=lambda *a, **kw: "",
            get_mode=lambda: "open-source",
            set_mode=lambda *a, **kw: None,
            describe_runtime_for=lambda *a, **kw: "",
            set_forced_model=lambda *a, **kw: None,
            clear_forced_model=lambda *a, **kw: None,
        )

    # Stub jarvis_agents and jarvis_watcher / jarvis_health / jarvis_executor imports
    for name in [
        "jarvis_agents", "jarvis_watcher", "jarvis_health", "jarvis_executor",
        "mem0_layer", "messages_thread",
    ]:
        _SAVED_MODULES[name] = sys.modules.get(name)
        if name not in sys.modules:
            _install_stub(name)


def _teardown_router_stubs():
    global _SAVED_MODULES, _saved_memory_track_topic, _saved_pm_parse, _saved_desktop_overlay
    for name, mod in _SAVED_MODULES.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod
    _mem = sys.modules.get("memory")
    if _mem is not None and _saved_memory_track_topic is not None:
        _mem.track_topic = _saved_memory_track_topic
    # Restore the original parse function on the real prompt_modifiers module.
    # Teardown restores the sys.modules reference but not the monkey-patched attribute.
    if _saved_pm_parse is not None:
        pm = sys.modules.get("prompt_modifiers")
        if pm is not None:
            pm.parse = _saved_pm_parse
    _saved_pm_parse = None
    # Sync desktop.overlay attribute on the real desktop module after sys.modules restore.
    # The main loop above restores sys.modules["desktop.overlay"] but the .overlay attribute
    # on the real desktop module still points to the stub we installed in setup.
    real_desktop = sys.modules.get("desktop")
    real_overlay = sys.modules.get("desktop.overlay")
    if real_desktop is not None and real_overlay is not None:
        real_desktop.overlay = real_overlay
    _saved_desktop_overlay = None
    for extra in ("desktop", "desktop.overlay", "model_router", "config", "router"):
        if extra not in _SAVED_MODULES:
            sys.modules.pop(extra, None)
    _SAVED_MODULES.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Task 1: Email digest detection
# ─────────────────────────────────────────────────────────────────────────────

class EmailDigestDetectionTests(unittest.TestCase):
    """_is_email_digest_query should match all required phrasings."""

    @classmethod
    def setUpClass(cls):
        _setup_router_stubs()

    @classmethod
    def tearDownClass(cls):
        _teardown_router_stubs()

    def setUp(self):
        import importlib
        sys.modules.pop("router", None)
        import router as r
        self.router = importlib.reload(r)
        self.detect = self.router._is_email_digest_query

    def test_what_are_my_emails_about_today(self):
        self.assertTrue(self.detect("what are my emails about today"))

    def test_any_important_emails(self):
        self.assertTrue(self.detect("any important emails?"))

    def test_any_emails(self):
        self.assertTrue(self.detect("any emails?"))

    def test_email_digest(self):
        self.assertTrue(self.detect("email digest"))

    def test_summarize_my_inbox(self):
        self.assertTrue(self.detect("summarize my inbox"))

    def test_summarize_emails(self):
        self.assertTrue(self.detect("summarize my emails"))

    def test_what_is_in_my_inbox(self):
        self.assertTrue(self.detect("what is in my inbox"))

    def test_email_summary(self):
        self.assertTrue(self.detect("email summary"))

    def test_no_false_positive_send_email(self):
        self.assertFalse(self.detect("send email to dad"))

    def test_no_false_positive_search(self):
        self.assertFalse(self.detect("find emails from john"))


# ─────────────────────────────────────────────────────────────────────────────
# Task 1: Email digest fast-path streams with "Email Digest" label
# ─────────────────────────────────────────────────────────────────────────────

class EmailDigestFastPathTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _setup_router_stubs()

    @classmethod
    def tearDownClass(cls):
        _teardown_router_stubs()

    def setUp(self):
        import importlib
        sys.modules.pop("router", None)
        import router as r
        self.router = importlib.reload(r)

    def _fake_email(self, sender="Alice", subject="Hello", snippet="Hi there"):
        return {"sender": sender, "subject": subject, "snippet": snippet}

    def test_label_is_email_digest(self):
        self.router.gs.get_unread_email_subjects.return_value = [self._fake_email()]
        with patch("brains.brain_ollama.ask_local_stream", return_value=iter(["• Bullet 1\n• Bullet 2\n• Bullet 3"])):
            stream, label = self.router.route_stream("any important emails?")
        self.assertEqual(label, "Email Digest")

    def test_calls_google_services(self):
        self.router.gs.get_unread_email_subjects.return_value = [self._fake_email()]
        with patch("brains.brain_ollama.ask_local_stream", return_value=iter(["summary"])):
            stream, label = self.router.route_stream("email digest")
            list(stream)
        self.router.gs.get_unread_email_subjects.assert_called()

    def test_offline_fallback_message(self):
        self.router.gs.get_unread_email_subjects.side_effect = Exception("offline")
        stream, label = self.router.route_stream("email digest")
        result = "".join(stream)
        self.assertIn("No email access available", result)

    def test_empty_inbox_message(self):
        self.router.gs.get_unread_email_subjects.return_value = []
        self.router.gs.get_unread_email_subjects.side_effect = None
        stream, label = self.router.route_stream("email digest")
        result = "".join(stream)
        self.assertIn("inbox is clear", result.lower())

    def test_llm_failure_falls_back_to_local_digest(self):
        """If ask_local_stream raises, the fallback _build_email_digest is used."""
        self.router.gs.get_unread_email_subjects.side_effect = None
        self.router.gs.get_unread_email_subjects.return_value = [
            self._fake_email("Bob", "Action Required", "please respond")
        ]

        def _raise(*a, **kw):
            raise RuntimeError("model not loaded")

        with patch("brains.brain_ollama.ask_local_stream", side_effect=_raise):
            stream, label = self.router.route_stream("summarize my inbox")
            result = "".join(stream)
        # Local _build_email_digest uses bullet format
        self.assertIn("•", result)


# ─────────────────────────────────────────────────────────────────────────────
# Task 2: _agent_calendar_upcoming
# ─────────────────────────────────────────────────────────────────────────────

class AgentCalendarUpcomingTests(unittest.TestCase):

    def setUp(self):
        self._saved_ja = sys.modules.get("jarvis_agents")

    def tearDown(self):
        # Restore jarvis_agents so other test modules' router._jagents binding stays valid.
        if self._saved_ja is not None:
            sys.modules["jarvis_agents"] = self._saved_ja
        else:
            sys.modules.pop("jarvis_agents", None)

    def _import_ja(self):
        # Import jarvis_agents directly (doesn't need router stubs)
        import importlib
        sys.modules.pop("jarvis_agents", None)
        import jarvis_agents
        return importlib.reload(jarvis_agents)

    def test_returns_formatted_calendar_text(self):
        ja = self._import_ja()
        mock_gs = MagicMock()
        mock_gs.get_week_events.return_value = [
            "Sat 7 Jun 10:00 AM — Team standup",
            "Sat 7 Jun 2:00 PM — Design review",
            "Sat 7 Jun 4:30 PM — 1:1 with manager",
        ]
        with patch.object(ja, "_safe_import", return_value=mock_gs):
            result = ja._agent_calendar_upcoming()
        self.assertEqual(result["agent"], "calendar_upcoming")
        self.assertEqual(result["status"], "ok")
        self.assertIn("10:00 AM", result["result"])
        self.assertIn("Team standup", result["result"])

    def test_handles_no_events_today(self):
        ja = self._import_ja()
        mock_gs = MagicMock()
        mock_gs.get_week_events.return_value = []
        with patch.object(ja, "_safe_import", return_value=mock_gs):
            result = ja._agent_calendar_upcoming()
        self.assertEqual(result["status"], "ok")
        self.assertIn("No events", result["result"])

    def test_handles_google_services_unavailable(self):
        ja = self._import_ja()
        with patch.object(ja, "_safe_import", return_value=None):
            result = ja._agent_calendar_upcoming()
        self.assertEqual(result["status"], "ok")
        self.assertIn("not connected", result["result"])

    def test_caps_at_three_events(self):
        ja = self._import_ja()
        mock_gs = MagicMock()
        mock_gs.get_week_events.return_value = [
            f"Sat 7 Jun {h}:00 AM — Event {i}"
            for i, h in enumerate([9, 10, 11, 14, 15], 1)
        ]
        with patch.object(ja, "_safe_import", return_value=mock_gs):
            result = ja._agent_calendar_upcoming()
        bullet_count = result["result"].count("•")
        self.assertLessEqual(bullet_count, 3)

    def test_error_returns_error_status(self):
        ja = self._import_ja()
        mock_gs = MagicMock()
        mock_gs.get_week_events.side_effect = RuntimeError("calendar API down")
        with patch.object(ja, "_safe_import", return_value=mock_gs):
            result = ja._agent_calendar_upcoming()
        self.assertEqual(result["status"], "error")


# ─────────────────────────────────────────────────────────────────────────────
# Task 2: _agent_pending_alerts
# ─────────────────────────────────────────────────────────────────────────────

class AgentPendingAlertsTests(unittest.TestCase):

    def setUp(self):
        self._saved_ja = sys.modules.get("jarvis_agents")

    def tearDown(self):
        if self._saved_ja is not None:
            sys.modules["jarvis_agents"] = self._saved_ja
        else:
            sys.modules.pop("jarvis_agents", None)

    def _import_ja(self):
        import importlib
        sys.modules.pop("jarvis_agents", None)
        import jarvis_agents
        return importlib.reload(jarvis_agents)

    def test_returns_empty_string_when_no_alerts(self):
        ja = self._import_ja()
        mock_pw = MagicMock()
        mock_pw.get_alerts.return_value = []
        with patch.object(ja, "_safe_import", return_value=mock_pw):
            result = ja._agent_pending_alerts()
        self.assertEqual(result["result"], "")
        self.assertFalse(result["escalate"])

    def test_returns_empty_when_proactive_watcher_unavailable(self):
        ja = self._import_ja()
        with patch.object(ja, "_safe_import", return_value=None):
            result = ja._agent_pending_alerts()
        self.assertEqual(result["result"], "")
        self.assertFalse(result["escalate"])

    def test_formats_alerts_when_present(self):
        ja = self._import_ja()
        mock_pw = MagicMock()
        mock_pw.get_alerts.return_value = [
            {"id": "1", "kind": "calendar", "message": "Team standup in 5 min", "dismissed": False},
            {"id": "2", "kind": "email", "message": "Urgent email from manager", "dismissed": False},
        ]
        with patch.object(ja, "_safe_import", return_value=mock_pw):
            result = ja._agent_pending_alerts()
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["escalate"])
        self.assertIn("Team standup in 5 min", result["result"])
        self.assertIn("Urgent email from manager", result["result"])

    def test_calls_get_alerts_with_include_dismissed_false(self):
        ja = self._import_ja()
        mock_pw = MagicMock()
        mock_pw.get_alerts.return_value = []
        with patch.object(ja, "_safe_import", return_value=mock_pw):
            ja._agent_pending_alerts()
        mock_pw.get_alerts.assert_called_once_with(include_dismissed=False)

    def test_error_returns_error_status(self):
        ja = self._import_ja()
        mock_pw = MagicMock()
        mock_pw.get_alerts.side_effect = RuntimeError("watcher crashed")
        with patch.object(ja, "_safe_import", return_value=mock_pw):
            result = ja._agent_pending_alerts()
        self.assertEqual(result["status"], "error")


if __name__ == "__main__":
    unittest.main()
