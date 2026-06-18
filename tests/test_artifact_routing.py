"""Tests for the Artifact tool — routing detection and handler output."""
import sys
import pathlib
import unittest
from unittest.mock import MagicMock, patch

# orchestrator imports cleanly with no stubs — no module-level sys.modules
# patching here. Stubs needed for router (Apple frameworks) are scoped to
# ArtifactHandlerTests via setUpClass/tearDownClass.
_APPLE_STUBS = ["AppKit", "Foundation", "Quartz", "objc"]

import orchestrator


class ArtifactOrchestrationTests(unittest.TestCase):
    """_fast_classify routes artifact-related inputs to the artifact tool."""

    def _classify(self, text: str):
        return orchestrator._fast_classify(text.lower().strip())

    def test_explicit_artifact_keyword_routes(self):
        d = self._classify("create an artifact for the auth flow")
        self.assertIsNotNone(d)
        self.assertEqual(d.tool, "artifact")

    def test_make_diagram_routes_to_artifact(self):
        d = self._classify("make a system diagram for the payment service")
        self.assertIsNotNone(d)
        self.assertEqual(d.tool, "artifact")

    def test_build_dashboard_routes_to_artifact(self):
        d = self._classify("build a dashboard showing test coverage")
        self.assertIsNotNone(d)
        self.assertEqual(d.tool, "artifact")

    def test_turn_into_artifact_routes(self):
        d = self._classify("turn this into an artifact")
        self.assertIsNotNone(d)
        self.assertEqual(d.tool, "artifact")

    def test_walkthrough_routes_to_artifact(self):
        d = self._classify("create a walkthrough for the onboarding flow")
        self.assertIsNotNone(d)
        self.assertEqual(d.tool, "artifact")

    def test_plain_question_does_not_route_to_artifact(self):
        d = self._classify("what time is it")
        self.assertIsNone(d)

    def test_confidence_is_high(self):
        d = self._classify("create an artifact")
        self.assertIsNotNone(d)
        self.assertGreaterEqual(d.confidence, 0.9)


class ArtifactHandlerTests(unittest.TestCase):
    """router artifact handler saves HTML and returns sensible voice output."""

    _pre_test_modules: set

    @classmethod
    def setUpClass(cls):
        # Snapshot sys.modules before installing anything so tearDownClass can
        # restore it exactly — preventing Apple framework stubs (and any modules
        # they transitively pull in, like ui.py) from contaminating later tests.
        cls._pre_test_modules = set(sys.modules.keys())
        for m in _APPLE_STUBS:
            if m not in sys.modules:
                sys.modules[m] = MagicMock()

    @classmethod
    def tearDownClass(cls):
        # Remove every module added since setUpClass (Apple stubs + transitive
        # imports like router, ui, etc.) so later test files see a clean state.
        added = set(sys.modules.keys()) - cls._pre_test_modules
        for m in added:
            sys.modules.pop(m, None)

    def _run_artifact(self, prompt: str, html_output: str = "<html>test</html>"):
        """Invoke the artifact route via route_stream and collect output."""
        with patch("provider_priority.ask_with_priority", return_value=html_output), \
             patch("notes.add_note"), \
             patch("pathlib.Path.write_text") as mock_write:
            import router
            stream, label = router.route_stream(prompt)
            text = "".join(stream)
            return text, label, mock_write

    def test_label_is_artifact(self):
        _, label, _ = self._run_artifact("create an artifact for the auth flow")
        self.assertEqual(label, "Artifact")

    def test_response_mentions_desktop(self):
        text, _, _ = self._run_artifact("make a diagram of the system")
        self.assertIn("Desktop", text)

    def test_html_file_is_written(self):
        _, _, mock_write = self._run_artifact("create a dashboard artifact")
        mock_write.assert_called_once()
        written_html = mock_write.call_args[0][0]
        self.assertIn("html", written_html)

    def test_graceful_on_generation_failure(self):
        with patch("provider_priority.ask_with_priority", side_effect=RuntimeError("model down")), \
             patch("notes.add_note"):
            import router
            stream, label = router.route_stream("create an artifact")
            text = "".join(stream)
        self.assertEqual(label, "Artifact")
        self.assertIn("error", text.lower())


if __name__ == "__main__":
    unittest.main()
