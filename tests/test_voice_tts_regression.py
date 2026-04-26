import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from types import SimpleNamespace

import voice


class VoiceTtsRegressionTests(unittest.TestCase):
    def setUp(self):
        self._voice_log_tmp = TemporaryDirectory()
        self._previous_voice_log_path = voice._VOICE_LOG_PATH
        voice._VOICE_LOG_PATH = Path(self._voice_log_tmp.name) / ".jarvis_voice.log"

    def tearDown(self):
        voice._VOICE_LOG_PATH = self._previous_voice_log_path
        self._voice_log_tmp.cleanup()
        voice._kokoro_disabled_reason = ""
        voice._mic_failure_cooldown_until = 0.0
        voice._mic_last_failure_detail = ""

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

    def test_speak_disables_kokoro_after_session_level_unavailable_error(self):
        voice._kokoro_disabled_reason = ""

        with patch("voice.call_privacy.should_suppress_audio", return_value=False), \
             patch("voice.TTS_BACKENDS", ("kokoro", "say")), \
             patch(
                 "voice.local_kokoro_tts.speak",
                 return_value={"ok": False, "engine": "kokoro", "error": "kokoro-onnx not installed"},
             ) as kokoro_mock, \
             patch("voice.local_tts.speak", return_value={"ok": True, "engine": "say"}) as local_mock:
            voice.speak("First beta response.")
            voice.speak("Second beta response.")

        kokoro_mock.assert_called_once_with("First beta response.")
        self.assertEqual(local_mock.call_count, 2)
        self.assertEqual(voice._kokoro_disabled_reason, "kokoro-onnx not installed")

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
        self.assertTrue(voice._wake_word_match("jarvis"))
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

    def test_wait_for_wake_word_honors_manual_trigger_already_set(self):
        voice._stop_requested.clear()
        voice._done_speaking.set()
        voice._manual_wake_trigger.set()
        try:
            with patch("voice._get_microphone", side_effect=AssertionError("should not open microphone when manual wake is already set")):
                voice.wait_for_wake_word()
        finally:
            voice._manual_wake_trigger.clear()

    def test_wait_for_wake_word_ignores_broken_pipe_from_debug_logging(self):
        voice._stop_requested.clear()
        voice._done_speaking.set()
        voice._manual_wake_trigger.set()
        try:
            with patch("builtins.print", side_effect=BrokenPipeError):
                voice.wait_for_wake_word()
        finally:
            voice._manual_wake_trigger.clear()

    def test_open_microphone_source_skips_candidates_without_live_stream(self):
        closed = []

        class _BadMic:
            def __enter__(self):
                return SimpleNamespace(stream=None, audio=SimpleNamespace(terminate=lambda: closed.append("bad-audio")))

            def __exit__(self, exc_type, exc, tb):
                closed.append("bad-exit")

        class _GoodMic:
            def __init__(self):
                self._stream = SimpleNamespace(close=lambda: closed.append("good-stream-close"))
                self._audio = SimpleNamespace(terminate=lambda: closed.append("good-audio"))

            def __enter__(self):
                return SimpleNamespace(stream=self._stream, audio=self._audio)

            def __exit__(self, exc_type, exc, tb):
                self._stream.close()
                self._audio.terminate()

        with patch("voice._microphone_candidates", return_value=[("Bad Mic", _BadMic()), ("Good Mic", _GoodMic())]):
            with voice._open_microphone_source() as source:
                self.assertIsNotNone(source.stream)

        self.assertIn("bad-audio", closed)
        self.assertIn("good-stream-close", closed)

    def test_open_microphone_source_cools_down_after_all_candidates_fail(self):
        class _BadMic:
            def __enter__(self):
                return SimpleNamespace(stream=None, audio=SimpleNamespace(terminate=lambda: None))

            def __exit__(self, exc_type, exc, tb):
                return None

        with patch("voice._microphone_candidates", return_value=[("Bad Mic", _BadMic())]):
            with self.assertRaises(RuntimeError):
                with voice._open_microphone_source():
                    pass

        with patch("voice._microphone_candidates", side_effect=AssertionError("cooldown should skip device opens")):
            with self.assertRaisesRegex(RuntimeError, "cooldown active"):
                with voice._open_microphone_source():
                    pass

    def test_microphone_candidates_prefer_real_inputs_and_skip_output_only_devices(self):
        names = [
            "MacBook Pro Speakers",
            "MacBook Pro Microphone",
            "BlackHole 2ch",
            "Microsoft Teams Audio",
        ]

        class _FakeMicrophone:
            def __init__(self, device_index=None):
                self.device_index = device_index

            @staticmethod
            def list_microphone_names():
                return names

        with patch("voice._input_capable_device_indexes", return_value={1, 3}), \
             patch("voice.sr.Microphone", _FakeMicrophone):
            candidates = voice._microphone_candidates()

        labels = [label for label, _ in candidates]
        indexes = [mic.device_index for _, mic in candidates]
        self.assertEqual(labels, ["MacBook Pro Microphone", "Default input device"])
        self.assertEqual(indexes, [1, None])

    def test_wait_for_wake_word_backs_off_after_microphone_open_failure(self):
        voice._stop_requested.clear()
        voice._done_speaking.set()
        voice._manual_wake_trigger.clear()

        def _stop_after_sleep(seconds):
            self.assertEqual(seconds, voice._MIC_OPEN_RETRY_SECONDS)
            voice._stop_requested.set()

        try:
            with patch("voice._open_microphone_source", side_effect=RuntimeError("AUHAL unavailable")), \
                 patch("voice._debug_log"), \
                 patch("voice._time.sleep", side_effect=_stop_after_sleep) as sleep_mock:
                voice.wait_for_wake_word()
        finally:
            voice._stop_requested.clear()

        sleep_mock.assert_called_once()

    def test_capture_audio_window_records_fixed_window(self):
        source = object()
        fallback_audio = object()

        with patch.object(voice._recognizer, "record", return_value=fallback_audio) as record_mock:
            audio = voice._capture_audio_window(
                source,
                duration=2.5,
                reason="wake word",
            )

        self.assertIs(audio, fallback_audio)
        record_mock.assert_called_once_with(source, duration=2.5)

    def test_listen_uses_fixed_window_fallback_when_phrase_detection_times_out(self):
        fake_audio = SimpleNamespace(get_wav_data=lambda: b"RIFFfake")

        class _GoodMic:
            def __enter__(self):
                return SimpleNamespace(stream=object(), audio=object())

            def __exit__(self, exc_type, exc, tb):
                return None

        with patch("voice._open_microphone_source", return_value=_GoodMic()), \
             patch("voice._ensure_calibrated"), \
             patch("voice._capture_audio_window", return_value=fake_audio) as capture_mock, \
             patch("voice._transcribe_wav_bytes", return_value="what time is it"), \
             patch("voice._time.sleep"):
            text = voice.listen()

        self.assertEqual(text, "what time is it")
        capture_mock.assert_called_once()
        self.assertEqual(capture_mock.call_args.kwargs["duration"], voice.MANUAL_PROMPT_WINDOW_SECONDS)


if __name__ == "__main__":
    unittest.main()
