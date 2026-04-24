"""Targeted tests for message recipient/body parsing with modifiers and delimiters.

Tests that _parse_message_compose correctly handles:
  - Channel hints (using iMessage, via SMS, etc.)
  - Scope hints (in my contacts, from contacts, etc.)
  - Body delimiters (comma-separated, colon-separated introductions)
  - Backward compatibility with space-delimited messages
"""
from __future__ import annotations

import sys
import types
import unittest


def _install_stub(name: str, **attrs) -> types.ModuleType:
    """Install a minimal stub module to satisfy imports."""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# Install ALL stubs BEFORE any imports of router, following test_eval_delta pattern
_SAVED_MODULES = {}

def _setup_stubs():
    global _SAVED_MODULES
    if _SAVED_MODULES:
        return  # Already setup

    # List of modules that router imports which may not be available in test env
    heavy_deps = [
        "speech_recognition", "ollama", "pyaudio", "sounddevice",
        "anthropic", "uvicorn", "sklearn", "transformers",
        "tools", "terminal", "browser", "desktop", "notes", "google_services",
        "camera", "meeting_listener", "memory", "memory_layer", "evals",
        "skills", "vault", "vault_capture", "source_ingest", "skill_factory",
        "interview_profile", "specialized_agents", "behavior_hooks",
        "capability_evals", "capability_parity", "cost_policy",
        "context_budget", "coder_workbench", "external_agent_patterns",
        "production_readiness", "security_roe", "usage_tracker",
        "prompt_modifiers", "self_improve", "hardware", "runtime_state",
        "messages", "call_privacy", "provider_router", "semantic_memory",
        "model_router"
    ]
    for name in heavy_deps:
        _SAVED_MODULES[name] = sys.modules.get(name)
        if name not in sys.modules:
            # For submodules, also stub the parent if needed
            if "." in name:
                parent = name.split(".")[0]
                if parent not in sys.modules:
                    _install_stub(parent)
            _install_stub(name)

    # Stub local_runtime submodules. Track the parent package too so we can
    # fully restore it on tearDown (otherwise a __path__-less stub poisons
    # later test files that try to import the real local_runtime.local_training).
    _SAVED_MODULES["local_runtime"] = sys.modules.get("local_runtime")
    for name in ["local_runtime.local_training", "local_runtime.local_model_eval",
                 "local_runtime.local_model_automation", "local_runtime.local_beta",
                 "local_runtime.local_model_benchmark", "local_runtime.model_fleet"]:
        _SAVED_MODULES[name] = sys.modules.get(name)
        if name not in sys.modules:
            parent = name.split(".")[0]
            if parent not in sys.modules:
                parent_mod = _install_stub(parent)
                parent_mod.__path__ = []  # mark as a package so submodule reimport works
            _install_stub(name)

    # Stub desktop.overlay specifically
    if "desktop" not in sys.modules:
        _install_stub("desktop", overlay=_install_stub("desktop.overlay"))
    else:
        sys.modules["desktop"].overlay = _install_stub("desktop.overlay")

    # Stub config with minimal values
    if "config" not in sys.modules:
        _install_stub("config", OPUS="gpt-4o", SONNET="claude-3-5-sonnet-20241022")

    # Stub model_router with needed functions BEFORE router imports it
    if "model_router" not in sys.modules or not hasattr(sys.modules["model_router"], "smart_stream"):
        _install_stub("model_router",
                      smart_stream=lambda *a, **kw: "",
                      format_with_mini=lambda *a, **kw: "",
                      get_mode=lambda: "open-source",
                      set_mode=lambda *a, **kw: None,
                      describe_runtime_for=lambda *a, **kw: "")


class MessageIntentParsingTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """Setup stubs before any tests run."""
        _setup_stubs()

    @classmethod
    def tearDownClass(cls):
        """Restore any sys.modules entries we shadowed so other test files
        (e.g. test_teacher_capture, test_eval_delta) see the real modules.
        Without this the stub for `local_runtime.local_training` survives and
        breaks `patch('local_runtime.local_training.record_teacher_example')`.
        """
        global _SAVED_MODULES
        for name, mod in _SAVED_MODULES.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        # Also drop the stubbed `desktop` / `desktop.overlay` and `model_router`
        # / `config` we installed unconditionally so the next test file can
        # import the real ones if it needs them.
        for extra in ("desktop", "desktop.overlay", "model_router", "config", "router"):
            if extra not in _SAVED_MODULES:
                sys.modules.pop(extra, None)
        _SAVED_MODULES = {}

    def setUp(self):
        """Per-test setup: reload router."""
        import importlib
        # Clean router from cache if it's there
        sys.modules.pop("router", None)
        # Import fresh router
        import router
        self.router = importlib.reload(router)

    def tearDown(self):
        """Per-test cleanup."""
        pass

    def test_message_with_contact_scope_and_channel_hints(self):
        """send a message to dad in my contacts using iMessage, to get milk
        -> recipient='dad', body='get milk'
        """
        result = self.router._parse_message_compose(
            "send a message to dad in my contacts using iMessage, to get milk"
        )
        self.assertIsNotNone(result)
        recipient, body = result
        self.assertEqual(recipient, "dad")
        self.assertEqual(body, "get milk")

    def test_message_via_sms_with_saying_delimiter(self):
        """send a message to mom via SMS, saying I'll be late
        -> recipient='mom', body="I'll be late"
        """
        result = self.router._parse_message_compose(
            "send a message to mom via SMS, saying I'll be late"
        )
        self.assertIsNotNone(result)
        recipient, body = result
        self.assertEqual(recipient, "mom")
        self.assertEqual(body, "I'll be late")

    def test_text_on_imessage_with_colon_delimiter(self):
        """text Alex on iMessage: meeting moved to 3pm
        -> recipient='Alex', body='meeting moved to 3pm'
        """
        result = self.router._parse_message_compose(
            "text Alex on iMessage: meeting moved to 3pm"
        )
        self.assertIsNotNone(result)
        recipient, body = result
        self.assertEqual(recipient, "Alex")
        self.assertEqual(body, "meeting moved to 3pm")

    def test_message_with_telling_him_delimiter(self):
        """send a message to dad, telling him happy birthday
        -> recipient='dad', body='happy birthday'
        """
        result = self.router._parse_message_compose(
            "send a message to dad, telling him happy birthday"
        )
        self.assertIsNotNone(result)
        recipient, body = result
        self.assertEqual(recipient, "dad")
        self.assertEqual(body, "happy birthday")

    def test_message_with_telling_him_without_comma(self):
        result = self.router._parse_message_compose(
            "send a message to dad telling him happy birthday"
        )
        self.assertIsNotNone(result)
        recipient, body = result
        self.assertEqual(recipient, "dad")
        self.assertEqual(body, "happy birthday")

    def test_message_with_that_delimiter(self):
        """message Sarah, that the package arrived
        -> recipient='Sarah', body='the package arrived'
        """
        result = self.router._parse_message_compose(
            "message Sarah, that the package arrived"
        )
        self.assertIsNotNone(result)
        recipient, body = result
        self.assertEqual(recipient, "Sarah")
        self.assertEqual(body, "the package arrived")

    def test_message_with_that_without_comma(self):
        result = self.router._parse_message_compose(
            "message Sarah that the package arrived"
        )
        self.assertIsNotNone(result)
        recipient, body = result
        self.assertEqual(recipient, "Sarah")
        self.assertEqual(body, "the package arrived")

    def test_backward_compat_space_delimited(self):
        """text Alex hello there
        -> recipient='Alex', body='hello there'
        (existing behavior should still work)
        """
        result = self.router._parse_message_compose(
            "text Alex hello there"
        )
        self.assertIsNotNone(result)
        recipient, body = result
        self.assertEqual(recipient, "Alex")
        self.assertEqual(body, "hello there")

    def test_send_text_message_to_recipient_strips_leading_to_from_body(self):
        result = self.router._parse_message_compose(
            "Send a text message to dad to get chocolate milk"
        )
        self.assertIsNotNone(result)
        recipient, body = result
        self.assertEqual(recipient, "dad")
        self.assertEqual(body, "get chocolate milk")

    def test_text_my_dad_phrase_normalizes_relationship_recipient(self):
        result = self.router._parse_message_compose(
            "text my dad to get milk"
        )
        self.assertIsNotNone(result)
        recipient, body = result
        self.assertEqual(recipient, "dad")
        self.assertEqual(body, "get milk")

    def test_message_recipient_correction_parses_send_it_to_instead(self):
        result = self.router._parse_message_recipient_correction(
            "send it to mom instead"
        )
        self.assertEqual(result, "mom")

    def test_message_recipient_correction_parses_actually_name(self):
        result = self.router._parse_message_recipient_correction("actually dad")
        self.assertEqual(result, "dad")

    def test_message_multiword_name_without_delimiter_uses_name_prefix(self):
        result = self.router._parse_message_compose(
            "message Aman Imran Hello"
        )
        self.assertIsNotNone(result)
        recipient, body = result
        self.assertEqual(recipient, "Aman Imran")
        self.assertEqual(body, "Hello")

    def test_negative_open_contacts_list(self):
        """open my contacts list
        -> should NOT match (not a message compose)
        """
        result = self.router._parse_message_compose(
            "open my contacts list"
        )
        self.assertIsNone(result)

    def test_negative_sentence_word_recipient(self):
        """send a message to someone interesting
        -> should NOT match (someone/interesting aren't real names)
        """
        result = self.router._parse_message_compose(
            "send a message to someone interesting"
        )
        self.assertIsNone(result)

    def test_message_multiple_modifiers_stripped(self):
        """send a message to john from my contacts using iMessage, to remind him of dinner
        -> recipient='john', body='remind him of dinner'
        """
        result = self.router._parse_message_compose(
            "send a message to john from my contacts using iMessage, to remind him of dinner"
        )
        self.assertIsNotNone(result)
        recipient, body = result
        self.assertEqual(recipient, "john")
        self.assertEqual(body, "remind him of dinner")

    def test_message_via_text_channel_hint(self):
        """send a message to emma via text, saying hello
        -> recipient='emma', body='hello'
        """
        result = self.router._parse_message_compose(
            "send a message to emma via text, saying hello"
        )
        self.assertIsNotNone(result)
        recipient, body = result
        self.assertEqual(recipient, "emma")
        self.assertEqual(body, "hello")

    def test_message_through_messages_app(self):
        """text bob through Messages: don't forget tomorrow
        -> recipient='bob', body="don't forget tomorrow"
        """
        result = self.router._parse_message_compose(
            "text bob through Messages: don't forget tomorrow"
        )
        self.assertIsNotNone(result)
        recipient, body = result
        self.assertEqual(recipient, "bob")
        self.assertEqual(body, "don't forget tomorrow")


if __name__ == "__main__":
    unittest.main()
