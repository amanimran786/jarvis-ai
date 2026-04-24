"""Targeted tests for brains._teacher_capture.

Covers the streaming wrapper used by both provider_priority (one-shot lane)
and model_router (streaming lane). Imports are deliberately minimal so this
test runs without anthropic/openai/sounddevice/etc. installed.
"""
from __future__ import annotations

import os
import types
import unittest
from unittest.mock import patch


def _candidate(provider="openai", model="gpt-4o"):
    return types.SimpleNamespace(provider=provider, model=model)


class TeacherCaptureGateTests(unittest.TestCase):

    def test_disabled_by_default(self):
        from brains import _teacher_capture
        with patch.dict(os.environ, {"JARVIS_TEACHER_CAPTURE": ""}, clear=False):
            self.assertFalse(_teacher_capture.is_enabled())

    def test_enabled_with_truthy_values(self):
        from brains import _teacher_capture
        for value in ("1", "true", "yes", "on", "TRUE"):
            with patch.dict(os.environ, {"JARVIS_TEACHER_CAPTURE": value}, clear=False):
                self.assertTrue(_teacher_capture.is_enabled(), value)

    def test_capture_noop_when_disabled(self):
        from brains import _teacher_capture
        with patch.dict(os.environ, {"JARVIS_TEACHER_CAPTURE": ""}, clear=False), \
             patch("local_runtime.local_training.record_teacher_example") as record:
            _teacher_capture.capture("p", "a", tier="strong", provider="openai", model="gpt-4o")
        record.assert_not_called()

    def test_capture_skips_low_tier(self):
        from brains import _teacher_capture
        with patch.dict(os.environ, {"JARVIS_TEACHER_CAPTURE": "1"}, clear=False), \
             patch("local_runtime.local_training.record_teacher_example") as record:
            _teacher_capture.capture("p", "a", tier="cheap", provider="openai", model="gpt-4o-mini")
        record.assert_not_called()

    def test_capture_records_strong_tier_when_enabled(self):
        from brains import _teacher_capture
        with patch.dict(os.environ, {"JARVIS_TEACHER_CAPTURE": "1"}, clear=False), \
             patch("local_runtime.local_training.record_teacher_example",
                   return_value={"ok": True}) as record:
            _teacher_capture.capture("p", "a", tier="strong", provider="openai", model="gpt-4o")
        record.assert_called_once()
        kwargs = record.call_args.kwargs
        self.assertEqual(kwargs["tags"], ["tier:strong", "provider:openai", "model:gpt-4o"])

    def test_capture_tolerates_none_or_non_dict_result(self):
        from brains import _teacher_capture
        for bad in (None, "ok", 42, ["ok"]):
            with patch.dict(os.environ, {"JARVIS_TEACHER_CAPTURE": "1"}, clear=False), \
                 patch("local_runtime.local_training.record_teacher_example",
                       return_value=bad):
                # Must not raise even though .get('ok') would fail on these.
                _teacher_capture.capture("p", "a", tier="strong",
                                         provider="openai", model="gpt-4o")


class TeacherCaptureWrapStreamTests(unittest.TestCase):

    def test_successful_stream_captures_full_answer(self):
        from brains import _teacher_capture
        chunks = ["hel", "lo ", "world"]
        with patch.dict(os.environ, {"JARVIS_TEACHER_CAPTURE": "1"}, clear=False), \
             patch("local_runtime.local_training.record_teacher_example",
                   return_value={"ok": True}) as record:
            out = list(_teacher_capture.wrap_stream(
                "the prompt", "strong", _candidate(), iter(chunks),
            ))
        self.assertEqual(out, chunks)
        record.assert_called_once()
        # First positional arg is the prompt; second is the answer.
        args, kwargs = record.call_args
        self.assertEqual(args[0], "the prompt")
        self.assertEqual(args[1], "hello world")

    def test_mid_stream_exception_does_not_capture(self):
        from brains import _teacher_capture

        def boom_after_one():
            yield "part"
            raise RuntimeError("upstream died")

        emitted = []
        with patch.dict(os.environ, {"JARVIS_TEACHER_CAPTURE": "1"}, clear=False), \
             patch("local_runtime.local_training.record_teacher_example") as record:
            with self.assertRaises(RuntimeError):
                for c in _teacher_capture.wrap_stream(
                    "p", "strong", _candidate(), boom_after_one(),
                ):
                    emitted.append(c)
        self.assertEqual(emitted, ["part"])
        record.assert_not_called()

    def test_low_tier_stream_passes_through_without_capture(self):
        from brains import _teacher_capture
        with patch.dict(os.environ, {"JARVIS_TEACHER_CAPTURE": "1"}, clear=False), \
             patch("local_runtime.local_training.record_teacher_example") as record:
            out = list(_teacher_capture.wrap_stream(
                "p", "cheap", _candidate(), iter(["a", "b"]),
            ))
        self.assertEqual(out, ["a", "b"])
        record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
