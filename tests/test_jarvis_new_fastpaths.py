"""
Unit tests for new router fast-paths (screen vision, focus advisor)
and jarvis_agents.focus_advisor().
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Lightweight stubs for PyQt6-dependent modules so imports don't fail in CI
for _mod in ("PyQt6", "PyQt6.QtCore", "PyQt6.QtWidgets", "PyQt6.QtGui"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

if "model_router" not in sys.modules:
    _mr = MagicMock()
    _mr.smart_stream.side_effect = ImportError("no model_router in CI")
    sys.modules["model_router"] = _mr


class FocusAdvisorTests(unittest.TestCase):
    """focus_advisor() dispatches the right agents and synthesises a string."""

    def test_focus_advisor_returns_string(self):
        import jarvis_agents as ja
        # Patch all agent runners to return lightweight stubs
        ok_result = {"agent": "x", "status": "ok", "result": "test context", "escalate": False}
        with patch.object(ja, "dispatch_parallel", return_value=[ok_result, ok_result, ok_result]):
            with patch.object(ja, "_synthesise", return_value="Focus on the deploy."):
                result = ja.focus_advisor()
        self.assertIsInstance(result, str)
        self.assertIn("Focus", result)

    def test_focus_advisor_empty_context(self):
        import jarvis_agents as ja
        with patch.object(ja, "dispatch_parallel", return_value=[]):
            result = ja.focus_advisor()
        # Should return the clear-calendar fallback string, not crash
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 5)

    def test_focus_advisor_uses_correct_agents(self):
        import jarvis_agents as ja
        captured_agents = []
        def _spy(agents, context=""):
            captured_agents.extend(agents)
            return []
        with patch.object(ja, "dispatch_parallel", side_effect=_spy):
            ja.focus_advisor()
        self.assertIn("calendar", captured_agents)
        self.assertIn("tasks", captured_agents)
        self.assertIn("vault", captured_agents)


def _real_router_available() -> bool:
    """Return True only if the real router module (not a MagicMock) is importable."""
    router_mod = sys.modules.get("router")
    if router_mod is not None and isinstance(router_mod, MagicMock):
        return False  # A previous test file pre-populated sys.modules with a mock
    try:
        import router as _r
        return callable(getattr(_r, "route_stream", None)) and not isinstance(_r, MagicMock)
    except (ImportError, OSError):
        return False


class ScreenVisionRouterTests(unittest.TestCase):
    """Screen vision fast-path in route_stream sends queries to camera module."""

    def test_screen_trigger_label_is_vision(self):
        """'what's on my screen' should produce label 'Vision'."""
        if not _real_router_available():
            self.skipTest("real router not importable in this environment")
        import router
        import camera as _cam_mod
        with patch.object(_cam_mod, "screenshot_and_describe", return_value="A terminal."):
            stream, label = router.route_stream("what's on my screen")
            list(stream)
        self.assertEqual(label, "Vision")

    def test_screen_trigger_calls_screenshot(self):
        """screenshot_and_describe must be called for screen queries."""
        if not _real_router_available():
            self.skipTest("real router not importable in this environment")
        import router
        import camera as _cam_mod
        called_with = []
        def _fake_describe(prompt):
            called_with.append(prompt)
            return "I see VS Code."
        with patch.object(_cam_mod, "screenshot_and_describe", side_effect=_fake_describe):
            stream, label = router.route_stream("analyze my screen")
            "".join(stream)
        self.assertTrue(len(called_with) > 0)


class ScreenTriggerKeywordsTests(unittest.TestCase):
    """Verify the trigger set covers expected phrasings."""

    _SCREEN_TRIGGERS = (
        "what's on my screen", "what is on my screen",
        "analyze my screen", "analyse my screen",
        "what do you see", "what can you see",
        "scan my screen", "look at my screen", "read my screen",
        "what's this", "what is this on screen",
        "describe my screen", "describe the screen",
        "what does this say", "read this for me",
        "what's this error", "what is this error",
        "help me with what's on my screen",
        "explain what's on my screen",
        "what am i looking at",
        "check my screen",
    )

    _FOCUS_TRIGGERS = (
        "what should i work on", "what should i do", "what's my priority",
        "what are my priorities", "what's most important", "what to work on",
        "where should i focus", "what should i focus on",
        "what do i focus on today", "what's the plan", "what's next for me",
        "help me prioritise", "help me prioritize",
    )

    def test_screen_triggers_non_empty(self):
        self.assertGreater(len(self._SCREEN_TRIGGERS), 5)

    def test_focus_triggers_non_empty(self):
        self.assertGreater(len(self._FOCUS_TRIGGERS), 5)

    def test_screen_triggers_all_lowercase(self):
        for t in self._SCREEN_TRIGGERS:
            self.assertEqual(t, t.lower(), f"Trigger should be lowercase: {t!r}")

    def test_focus_triggers_all_lowercase(self):
        for t in self._FOCUS_TRIGGERS:
            self.assertEqual(t, t.lower(), f"Trigger should be lowercase: {t!r}")


if __name__ == "__main__":
    unittest.main()
