import unittest
from unittest.mock import patch

import voice


class VoiceTtsRegressionTests(unittest.TestCase):
    def test_speak_prefers_local_tts_before_paid_fallbacks(self):
        with patch("voice.call_privacy.should_suppress_audio", return_value=False), \
             patch("voice.TTS_BACKENDS", ("say", "elevenlabs", "openai")), \
             patch("voice.local_tts.speak", return_value={"ok": True, "engine": "say"}) as local_mock, \
             patch("voice._speak_elevenlabs") as eleven_mock, \
             patch("voice._speak_openai") as openai_mock:
            voice.speak("Testing primary TTS.")

        local_mock.assert_called_once_with("Testing primary TTS.")
        eleven_mock.assert_not_called()
        openai_mock.assert_not_called()
        self.assertTrue(voice._done_speaking.is_set())

    def test_speak_falls_back_to_elevenlabs_when_local_tts_fails(self):
        with patch("voice.call_privacy.should_suppress_audio", return_value=False), \
             patch("voice.TTS_BACKENDS", ("say", "elevenlabs", "openai")), \
             patch("voice.local_tts.speak", return_value={"ok": False, "engine": "say", "error": "say unavailable"}) as local_mock, \
             patch("voice._speak_elevenlabs", return_value=False) as eleven_mock, \
             patch("voice._speak_openai") as openai_mock:
            voice.speak("Fallback path.")

        local_mock.assert_called_once_with("Fallback path.")
        eleven_mock.assert_called_once_with("Fallback path.")
        openai_mock.assert_called_once_with("Fallback path.")
        self.assertTrue(voice._done_speaking.is_set())

    def test_speak_skips_tts_entirely_when_audio_is_suppressed(self):
        with patch("voice.call_privacy.should_suppress_audio", return_value=True), \
             patch("voice.local_tts.speak") as local_mock, \
             patch("voice._speak_elevenlabs") as eleven_mock, \
             patch("voice._speak_openai") as openai_mock:
            voice.speak("This should stay silent.")

        local_mock.assert_not_called()
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

    def test_wake_word_match_handles_exact_prefix_and_suffix(self):
        self.assertTrue(voice._wake_word_match("hey jarvis"))
        self.assertTrue(voice._wake_word_match("hey jarvis open notes"))
        self.assertTrue(voice._wake_word_match("can you help me ok jarvis"))
        self.assertFalse(voice._wake_word_match("hello there"))

    def test_transcribe_wake_audio_prefers_local_stt_before_google(self):
        class FakeAudio:
            def get_wav_data(self):
                return b"RIFFfake"

        with patch("voice.local_stt.transcribe_file", return_value={"ok": True, "text": "hey jarvis", "engine": "faster-whisper"}), \
             patch("voice._recognizer.recognize_google", side_effect=AssertionError("should not call google")):
            text = voice._transcribe_wake_audio(FakeAudio())

        self.assertEqual(text, "hey jarvis")

    def test_transcribe_wake_audio_skips_google_fallback_in_open_source_mode(self):
        class FakeAudio:
            def get_wav_data(self):
                return b"RIFFfake"

        with patch("voice.local_stt.transcribe_file", return_value={"ok": False, "text": "", "error": "empty transcript"}), \
             patch("model_router.is_open_source_mode", return_value=True), \
             patch("voice._recognizer.recognize_google", side_effect=AssertionError("should not call google")):
            text = voice._transcribe_wake_audio(FakeAudio())

        self.assertIsNone(text)


if __name__ == "__main__":
    unittest.main()
