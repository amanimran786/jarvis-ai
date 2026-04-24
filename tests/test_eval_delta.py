"""Targeted tests for eval_delta.run_delta / format_delta.

Mocks `capability_evals.list_cases`, `brains.brain_ollama.ask_local`, and
`provider_priority._ask_openai` so the test runs offline and never touches
real model lanes.
"""
from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch


def _install_stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


class EvalDeltaTests(unittest.TestCase):

    def setUp(self):
        # Stub brains.brain_ollama and provider_priority so eval_delta's lazy
        # imports succeed without dragging in heavy deps (anthropic, ollama).
        # Do NOT clobber the real `brains` package if it's importable — that
        # would break other tests' `from brains import _teacher_capture`.
        self._saved = {k: sys.modules.get(k) for k in
                       ("brains", "brains.brain_ollama", "provider_priority", "config")}
        if "brains" not in sys.modules:
            import importlib
            try:
                importlib.import_module("brains")
            except Exception:
                pkg = _install_stub("brains")
                pkg.__path__ = []
        self._saved_brain_ollama_attr = getattr(sys.modules.get("brains"), "brain_ollama", None)
        self._ollama_calls = []
        self._openai_calls = []

        def fake_ask_local(prompt, model=None, raise_on_error=False):
            self._ollama_calls.append((prompt, model))
            return f"local::{prompt}"

        def fake_ask_openai(prompt, *, model, system_extra=""):
            self._openai_calls.append((prompt, model))
            return f"cloud::{prompt}"

        def fake_ask_with_priority(*a, **kw):  # for local_training import path
            return ""

        brain_ollama_stub = _install_stub("brains.brain_ollama", ask_local=fake_ask_local)
        if "brains" in sys.modules:
            setattr(sys.modules["brains"], "brain_ollama", brain_ollama_stub)
        _install_stub("provider_priority",
                      _ask_openai=fake_ask_openai,
                      ask_with_priority=fake_ask_with_priority)
        # Real `config` is a plain constants module — leave it alone unless it
        # genuinely can't import (in that case fall back to a stub).
        if "config" not in sys.modules:
            try:
                import importlib
                importlib.import_module("config")
            except Exception:
                _install_stub("config", GPT_FULL="gpt-4o", LOCAL_REASONING="qwen2.5:7b")

    def tearDown(self):
        # Restore any modules we shadowed so other tests see the real ones.
        for name, mod in self._saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        if "brains" in sys.modules and self._saved_brain_ollama_attr is not None:
            setattr(sys.modules["brains"], "brain_ollama", self._saved_brain_ollama_attr)

    def test_run_delta_runs_both_lanes(self):
        cases = [
            {"id": "c1", "group": "g1", "prompt": "what is 2+2"},
            {"id": "c2", "group": "g1", "prompt": "name the moons of mars"},
        ]
        with patch("capability_evals.list_cases", return_value=cases):
            import importlib
            import eval_delta
            importlib.reload(eval_delta)
            rows = eval_delta.run_delta(group="g1", limit=2, tier="strong")

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["local"], "local::what is 2+2")
        self.assertEqual(rows[0]["cloud"], "cloud::what is 2+2")
        self.assertEqual(rows[1]["local"], "local::name the moons of mars")
        self.assertEqual(self._ollama_calls[0][0], "what is 2+2")
        self.assertEqual(self._openai_calls[1][0], "name the moons of mars")

    def test_run_delta_captures_lane_errors(self):
        def boom_local(prompt, model=None, raise_on_error=False):
            raise RuntimeError("ollama down")

        brain_ollama_stub = _install_stub("brains.brain_ollama", ask_local=boom_local)
        if "brains" in sys.modules:
            setattr(sys.modules["brains"], "brain_ollama", brain_ollama_stub)
        cases = [{"id": "c1", "group": "g1", "prompt": "hi"}]
        with patch("capability_evals.list_cases", return_value=cases):
            import importlib
            import eval_delta
            importlib.reload(eval_delta)
            rows = eval_delta.run_delta(group="g1", limit=1)

        self.assertEqual(rows[0]["local"], "")
        self.assertIn("ollama down", rows[0]["local_error"])
        self.assertEqual(rows[0]["cloud"], "cloud::hi")

    def test_format_delta_handles_empty_and_populated(self):
        import eval_delta
        self.assertEqual(eval_delta.format_delta([]), "No eval cases matched.")
        rendered = eval_delta.format_delta([{
            "id": "c1", "group": "g1", "prompt": "hi",
            "local_model": "qwen2.5:7b", "cloud_model": "gpt-4o",
            "local": "hello", "cloud": "hi there",
        }])
        self.assertIn("--- g1/c1", rendered)
        self.assertIn("LOCAL:", rendered)
        self.assertIn("hello", rendered)
        self.assertIn("CLOUD:", rendered)
        self.assertIn("hi there", rendered)

    def test_format_delta_renders_error_when_lane_empty(self):
        import eval_delta
        rendered = eval_delta.format_delta([{
            "id": "c1", "group": "g1", "prompt": "hi",
            "local_model": "qwen2.5:7b", "cloud_model": "gpt-4o",
            "local": "", "local_error": "ollama down",
            "cloud": "ok",
        }])
        self.assertIn("ERROR: ollama down", rendered)


if __name__ == "__main__":
    unittest.main()
