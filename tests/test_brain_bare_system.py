"""bare_system=True must strip persona + user memory from utility LLM calls.

The verifier judge was inheriting SYSTEM_PROMPT (Jarvis persona) plus
mem.get_context() (~1.4K tokens combined) on every call — wasted tokens and
a calibration bias (the judge carried the persona of the agent it judges).
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import brains.brain as brain


def _fake_stream_chunks():
    delta = SimpleNamespace(content="ok.")
    choice = SimpleNamespace(delta=delta)
    return [SimpleNamespace(usage=None, choices=[choice])]


def _run_ask_stream(**kwargs):
    captured = {}
    fake_client = MagicMock()

    def fake_create(**create_kwargs):
        captured.update(create_kwargs)
        return _fake_stream_chunks()

    fake_client.chat.completions.create.side_effect = fake_create
    with patch.object(brain, "client", fake_client), \
         patch.object(brain.usage_tracker, "record"):
        list(brain.ask_stream("judge this", bypass_local=True, **kwargs))
    return captured["messages"]


class BareSystemTests(unittest.TestCase):
    def setUp(self):
        self._prompt = patch.object(brain, "SYSTEM_PROMPT", "Jarvis system prompt for tests.")
        self._prompt.start()

    def tearDown(self):
        self._prompt.stop()

    def test_bare_system_omits_persona_and_memory(self):
        with patch.object(brain.mem, "get_context") as mock_mem:
            messages = _run_ask_stream(bare_system=True)
        mock_mem.assert_not_called()
        self.assertEqual([m["role"] for m in messages], ["user"])

    def test_bare_system_keeps_explicit_system_extra(self):
        messages = _run_ask_stream(bare_system=True, system_extra="be strict")
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], "be strict")
        self.assertNotIn(brain.SYSTEM_PROMPT[:40], messages[0]["content"])

    def test_default_still_injects_persona_and_memory(self):
        with patch.object(brain.mem, "get_context", return_value="\nUSER MEMORY"):
            messages = _run_ask_stream()
        self.assertEqual(messages[0]["role"], "system")
        self.assertTrue(messages[0]["content"].startswith(brain.SYSTEM_PROMPT[:40]))
        self.assertIn("USER MEMORY", messages[0]["content"])


class VerifierUsesBareSystemTests(unittest.TestCase):
    def test_auto_verify_passes_bare_system(self):
        import task_runtime
        captured = {}

        def fake_ask_stream(prompt, **kw):
            captured.update(kw)
            return iter(['{"score": 1.0, "reason": "ok"}'])

        with patch("brains.brain.ask_stream", fake_ask_stream):
            task_runtime._auto_verify("t1", "qa-tester", "task", "result", tool_calls=1,
                                      tool_transcript="$ ls\nok")
        self.assertTrue(captured.get("bare_system"))
        self.assertTrue(captured.get("bypass_local"))


if __name__ == "__main__":
    unittest.main()
