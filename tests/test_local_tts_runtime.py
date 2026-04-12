import unittest
from unittest.mock import patch

from local_runtime import local_tts


class LocalTtsRuntimeTests(unittest.TestCase):
    def setUp(self):
        local_tts._VOICE_CACHE = None

    def test_configured_voice_prefers_requested_voice_when_available(self):
        with patch("local_runtime.local_tts.LOCAL_TTS_VOICE", "Reed (English (US))"), \
             patch("local_runtime.local_tts._available_voices", return_value=["Reed (English (US))", "Samantha"]):
            self.assertEqual(local_tts._configured_voice(), "Reed (English (US))")

    def test_configured_voice_falls_back_to_best_available_modern_voice(self):
        with patch("local_runtime.local_tts.LOCAL_TTS_VOICE", "Missing Voice"), \
             patch("local_runtime.local_tts._available_voices", return_value=["Flo (English (US))", "Samantha"]):
            self.assertEqual(local_tts._configured_voice(), "Flo (English (US))")

    def test_configured_rate_uses_less_robotic_default(self):
        with patch("local_runtime.local_tts.LOCAL_TTS_RATE_WPM", 175):
            self.assertEqual(local_tts._configured_rate(), 175)


if __name__ == "__main__":
    unittest.main()
