import unittest
from unittest.mock import patch

import voice


class VoiceTtsRegressionTests(unittest.TestCase):
    def test_speak_prefers_primary_tts_before_fallback(self):
        with patch("voice.call_privacy.should_suppress_audio", return_value=False), \
             patch("voice._speak_elevenlabs", return_value=True) as eleven_mock, \
             patch("voice._speak_openai") as openai_mock:
            voice.speak("Testing primary TTS.")

        eleven_mock.assert_called_once_with("Testing primary TTS.")
        openai_mock.assert_not_called()
        self.assertTrue(voice._done_speaking.is_set())

    def test_speak_falls_back_when_primary_tts_fails(self):
        with patch("voice.call_privacy.should_suppress_audio", return_value=False), \
             patch("voice._speak_elevenlabs", return_value=False) as eleven_mock, \
             patch("voice._speak_openai") as openai_mock:
            voice.speak("Fallback path.")

        eleven_mock.assert_called_once_with("Fallback path.")
        openai_mock.assert_called_once_with("Fallback path.")
        self.assertTrue(voice._done_speaking.is_set())

    def test_speak_skips_tts_entirely_when_audio_is_suppressed(self):
        with patch("voice.call_privacy.should_suppress_audio", return_value=True), \
             patch("voice._speak_elevenlabs") as eleven_mock, \
             patch("voice._speak_openai") as openai_mock:
            voice.speak("This should stay silent.")

        eleven_mock.assert_not_called()
        openai_mock.assert_not_called()
        self.assertTrue(voice._done_speaking.is_set())

    def test_speak_stream_splits_complete_sentences_without_breaking_decimals(self):
        spoken = []
        chunks = iter(["Pi is 3.14. Done!"])

        with patch("voice.call_privacy.should_suppress_audio", return_value=False), \
             patch("voice.speak", side_effect=lambda text: spoken.append(text)):
            full_text = voice.speak_stream(chunks)

        self.assertEqual(full_text, "Pi is 3.14. Done!")
        self.assertEqual(spoken, ["Pi is 3.14.", "Done!"])

    def test_speak_stream_suppressed_returns_full_text_without_audio_calls(self):
        chunks = iter(["First sentence. ", "Second sentence."])

        with patch("voice.call_privacy.should_suppress_audio", return_value=True), \
             patch("voice.speak") as speak_mock:
            full_text = voice.speak_stream(chunks)

        self.assertEqual(full_text, "First sentence. Second sentence.")
        speak_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
