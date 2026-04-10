import importlib
import os
import unittest
from unittest.mock import patch

import config as config_module


class LocalSttConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_env = dict(os.environ)

    def tearDown(self):
        with patch.dict(os.environ, self._original_env, clear=True):
            importlib.reload(config_module)

    def _reload_with_env(self, env: dict[str, str]):
        base_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
        }
        base_env.update(env)
        with patch.dict(os.environ, base_env, clear=True):
            return importlib.reload(config_module)

    def test_defaults_to_faster_whisper_first_with_openai_fallback(self):
        cfg = self._reload_with_env({})
        self.assertEqual(cfg.STT_BACKENDS, ("faster-whisper", "openai"))
        self.assertEqual(cfg.STT_PRIMARY_BACKEND, "faster-whisper")
        self.assertTrue(cfg.LOCAL_STT_ENABLED)
        self.assertTrue(cfg.OPENAI_STT_FALLBACK_ENABLED)

    def test_explicit_backend_order_filters_unknown_and_duplicates(self):
        cfg = self._reload_with_env({
            "JARVIS_STT_BACKENDS": "openai, faster-whisper, invalid, openai",
        })
        self.assertEqual(cfg.STT_BACKENDS, ("openai", "faster-whisper"))
        self.assertEqual(cfg.STT_PRIMARY_BACKEND, "openai")

    def test_disabling_local_stt_falls_back_to_openai(self):
        cfg = self._reload_with_env({
            "JARVIS_LOCAL_STT_ENABLED": "0",
            "JARVIS_STT_BACKENDS": "faster-whisper,openai",
        })
        self.assertEqual(cfg.STT_BACKENDS, ("openai",))
        self.assertEqual(cfg.STT_PRIMARY_BACKEND, "openai")
        self.assertFalse(cfg.LOCAL_STT_ENABLED)
        self.assertTrue(cfg.OPENAI_STT_FALLBACK_ENABLED)

    def test_runtime_config_exposes_faster_whisper_knobs(self):
        cfg = self._reload_with_env({
            "JARVIS_STT_LANGUAGE": "en",
            "JARVIS_FASTER_WHISPER_MODEL": "small.en",
            "JARVIS_FASTER_WHISPER_DEVICE": "cpu",
            "JARVIS_FASTER_WHISPER_COMPUTE_TYPE": "int8_float16",
            "JARVIS_FASTER_WHISPER_CPU_THREADS": "8",
            "JARVIS_FASTER_WHISPER_NUM_WORKERS": "3",
            "JARVIS_FASTER_WHISPER_VAD_FILTER": "0",
            "JARVIS_FASTER_WHISPER_BEAM_SIZE": "2",
        })
        runtime = cfg.stt_runtime_config()
        self.assertEqual(runtime["language"], "en")
        self.assertEqual(runtime["faster_whisper"]["model"], "small.en")
        self.assertEqual(runtime["faster_whisper"]["device"], "cpu")
        self.assertEqual(runtime["faster_whisper"]["compute_type"], "int8_float16")
        self.assertEqual(runtime["faster_whisper"]["cpu_threads"], 8)
        self.assertEqual(runtime["faster_whisper"]["num_workers"], 3)
        self.assertFalse(runtime["faster_whisper"]["vad_filter"])
        self.assertEqual(runtime["faster_whisper"]["beam_size"], 2)

    def test_system_prompt_enforces_truthful_scope_language(self):
        cfg = self._reload_with_env({})
        self.assertIn("Do not claim to be unrestricted", cfg.SYSTEM_PROMPT)
        self.assertIn("Never claim you can bypass runtime policy", cfg.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
