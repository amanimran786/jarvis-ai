import unittest
import os
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from types import SimpleNamespace

import operative
import voice
from harness import tts as progress_tts


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
        voice._MIC_RECENT_FAILURES.clear()
        voice._mic_open_worker = None

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

    def test_operative_speaks_completed_step_when_enabled(self):
        step = operative.Step(number=1, description="Write the report", tool="chat")

        with patch("operative.VOICE_ENABLED", True), \
             patch("operative.plan_task", return_value=[step]), \
             patch("operative.execute_step", return_value=(True, "report written")), \
             patch("operative.preflect.is_enabled", return_value=False), \
             patch("operative._summarize", return_value="Task complete."), \
             patch("operative.speak_step") as speak_mock:
            result = operative.run_task("Write a report")

        self.assertTrue(result["ok"])
        speak_mock.assert_called_once_with(1, "Write the report", ok=True)

    def test_operative_does_not_speak_completed_step_when_disabled(self):
        step = operative.Step(number=1, description="Write the report", tool="chat")

        with patch("operative.VOICE_ENABLED", False), \
             patch("operative.plan_task", return_value=[step]), \
             patch("operative.execute_step", return_value=(True, "report written")), \
             patch("operative.preflect.is_enabled", return_value=False), \
             patch("operative._summarize", return_value="Task complete."), \
             patch("operative.speak_step") as speak_mock:
            operative.run_task("Write a report")

        speak_mock.assert_not_called()

    def test_operative_step_tts_logs_failure_and_continues(self):
        with patch(
            "harness.tts.local_tts.speak",
            return_value={"ok": False, "error": "say unavailable"},
        ), patch("harness.tts.logging.exception") as log_mock:
            spoken = progress_tts.speak_step(2, "Run tests", ok=False)

        self.assertFalse(spoken)
        log_mock.assert_called_once()

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

    def test_transcribe_wav_bytes_skips_openai_for_local_empty_transcript(self):
        local_silence = {"ok": False, "error": "empty transcript"}

        with patch("voice.local_stt.transcribe_audio", return_value=local_silence), \
             patch("voice.local_stt.openai_fallback_allowed", return_value=True), \
             patch("voice._openai_client") as openai_client:
            text = voice._transcribe_wav_bytes(b"RIFFfake")

        self.assertIsNone(text)
        openai_client.audio.transcriptions.create.assert_not_called()

    def test_transcribe_audio_file_skips_openai_for_local_empty_transcript(self):
        local_silence = {"ok": False, "error": "empty transcript"}

        with TemporaryDirectory() as td:
            path = Path(td) / "silence.wav"
            path.write_bytes(b"RIFFfake")
            with patch("voice.local_stt.transcribe_file", return_value=local_silence), \
                 patch("voice.local_stt.openai_fallback_allowed", return_value=True), \
                 patch("voice._openai_client") as openai_client:
                text = voice._transcribe_audio_file(str(path))

        self.assertIsNone(text)
        openai_client.audio.transcriptions.create.assert_not_called()

    def test_transcribe_wav_bytes_preserves_openai_fallback_for_engine_failure(self):
        engine_failure = {"ok": False, "error": "local model unavailable"}

        with patch("voice.local_stt.transcribe_audio", return_value=engine_failure), \
             patch("voice.local_stt.openai_fallback_allowed", return_value=True), \
             patch("voice._openai_client") as openai_client:
            openai_client.audio.transcriptions.create.return_value = SimpleNamespace(
                text="remote transcript"
            )
            text = voice._transcribe_wav_bytes(b"RIFFfake")

        self.assertEqual(text, "remote transcript")
        openai_client.audio.transcriptions.create.assert_called_once()

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

    def test_transcribe_wake_audio_skips_google_for_local_empty_transcript(self):
        class FakeAudio:
            def get_wav_data(self):
                return b"RIFFfake"

        local_silence = {"ok": False, "error": "empty transcript"}
        with patch("voice.local_stt.transcribe_file", return_value=local_silence), \
             patch("model_router.is_open_source_mode", return_value=False), \
             patch("voice._recognizer.recognize_google") as google_mock:
            text = voice._transcribe_wake_audio(FakeAudio())

        self.assertIsNone(text)
        google_mock.assert_not_called()

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

    def test_open_microphone_source_suppresses_native_stderr_during_probe(self):
        calls = []

        class _Guard:
            def __enter__(self):
                calls.append("enter")

            def __exit__(self, exc_type, exc, tb):
                calls.append("exit")

        class _GoodMic:
            def __enter__(self):
                return SimpleNamespace(stream=SimpleNamespace(close=lambda: None), audio=SimpleNamespace(terminate=lambda: None))

            def __exit__(self, exc_type, exc, tb):
                return None

        with patch("voice._microphone_candidates", return_value=[("Good Mic", _GoodMic())]), \
             patch("voice._suppress_native_audio_stderr", return_value=_Guard()):
            with voice._open_microphone_source() as source:
                self.assertIsNotNone(source.stream)

        self.assertEqual(calls, ["enter", "exit"])

    def test_open_microphone_source_times_out_without_opening_next_candidate(self):
        release_open = threading.Event()
        opened = []

        class _BlockingMic:
            def __enter__(self):
                opened.append("blocking")
                release_open.wait()
                return SimpleNamespace(
                    stream=SimpleNamespace(close=lambda: None),
                    audio=SimpleNamespace(terminate=lambda: None),
                )

            def __exit__(self, exc_type, exc, tb):
                return None

        class _NextMic:
            def __enter__(self):
                opened.append("next")
                return SimpleNamespace(stream=object(), audio=object())

            def __exit__(self, exc_type, exc, tb):
                return None

        try:
            with patch(
                "voice._microphone_candidates",
                return_value=[("Blocking Mic", _BlockingMic()), ("Next Mic", _NextMic())],
            ), patch("voice._MIC_OPEN_TIMEOUT_SECONDS", 0.01):
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    with voice._open_microphone_source():
                        pass

            self.assertEqual(opened, ["blocking"])
            self.assertIsNotNone(voice._mic_open_worker)
            self.assertTrue(voice._mic_open_worker.is_alive())
        finally:
            release_open.set()
            worker = voice._mic_open_worker
            if worker is not None:
                worker.join(timeout=1.0)

    def test_native_audio_suppression_covers_stdout_and_stderr(self):
        with TemporaryDirectory() as tmp:
            stdout_path = Path(tmp) / "stdout.log"
            stderr_path = Path(tmp) / "stderr.log"
            saved_stdout = os.dup(1)
            saved_stderr = os.dup(2)
            stdout_fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
            stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
            try:
                os.dup2(stdout_fd, 1)
                os.dup2(stderr_fd, 2)
                with voice._suppress_native_audio_stderr():
                    os.write(1, b"hidden stdout\n")
                    os.write(2, b"hidden stderr\n")
                os.write(1, b"visible stdout\n")
                os.write(2, b"visible stderr\n")
            finally:
                os.dup2(saved_stdout, 1)
                os.dup2(saved_stderr, 2)
                os.close(saved_stdout)
                os.close(saved_stderr)
                os.close(stdout_fd)
                os.close(stderr_fd)

            self.assertEqual(stdout_path.read_text(), "visible stdout\n")
            self.assertEqual(stderr_path.read_text(), "visible stderr\n")

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
             patch("voice._default_input_device_info", return_value=None), \
             patch("voice.sr.Microphone", _FakeMicrophone):
            candidates = voice._microphone_candidates()

        labels = [label for label, _ in candidates]
        indexes = [mic.device_index for _, mic in candidates]
        self.assertEqual(labels, ["MacBook Pro Microphone", "Default input device"])
        self.assertEqual(indexes, [1, None])

    def test_microphone_candidates_filter_virtual_devices_from_all_tiers(self):
        names = [
            "Aggregate Device",
            "BlackHole 2ch",
            "AirPods Pro Virtual Mic",
            "MacBook Pro Microphone",
        ]

        class _FakeMicrophone:
            def __init__(self, device_index=None):
                self.device_index = device_index

            @staticmethod
            def list_microphone_names():
                return names

        with patch("voice._input_capable_device_indexes", return_value={0, 1, 2, 3}), \
             patch("voice._default_input_device_info", return_value=None), \
             patch("voice.sr.Microphone", _FakeMicrophone):
            candidates = voice._microphone_candidates()

        labels = [label for label, _ in candidates]
        indexes = [mic.device_index for _, mic in candidates]
        self.assertEqual(labels, ["MacBook Pro Microphone", "Default input device"])
        self.assertEqual(indexes, [3, None])

    def test_microphone_candidates_skip_virtual_default_input_device(self):
        names = ["BlackHole 2ch", "Aggregate Device"]

        class _FakeMicrophone:
            def __init__(self, device_index=None):
                self.device_index = device_index

            @staticmethod
            def list_microphone_names():
                return names

        with patch("voice._input_capable_device_indexes", return_value={0, 1}), \
             patch(
                 "voice._default_input_device_info",
                 return_value={"index": 1, "name": "Aggregate Device", "maxInputChannels": 2},
             ), \
             patch("voice.sr.Microphone", _FakeMicrophone):
            candidates = voice._microphone_candidates()

        self.assertEqual(candidates, [])

    def test_open_microphone_source_skips_recently_failed_candidate_on_next_open(self):
        opened = []

        class _BadMic:
            def __enter__(self):
                opened.append("bad")
                raise RuntimeError("AUHAL unavailable")

            def __exit__(self, exc_type, exc, tb):
                return None

        class _GoodMic:
            def __enter__(self):
                opened.append("good")
                return SimpleNamespace(stream=SimpleNamespace(close=lambda: None), audio=SimpleNamespace(terminate=lambda: None))

            def __exit__(self, exc_type, exc, tb):
                return None

        candidates = [("Bad Mic", _BadMic()), ("Good Mic", _GoodMic())]
        with patch("voice._microphone_candidates", return_value=candidates):
            with voice._open_microphone_source():
                pass
            with voice._open_microphone_source():
                pass

        self.assertEqual(opened, ["bad", "good", "good"])

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

    def test_wait_for_wake_word_closes_mic_between_wake_windows(self):
        voice._stop_requested.clear()
        voice._done_speaking.set()
        voice._manual_wake_trigger.clear()
        events = []
        fake_audio = object()

        class _MicWindow:
            def __enter__(self):
                events.append("enter")
                return object()

            def __exit__(self, exc_type, exc, tb):
                events.append("exit")
                return None

        try:
            with patch("voice._open_microphone_source", side_effect=[_MicWindow(), _MicWindow()]), \
                 patch("voice._ensure_calibrated"), \
                 patch("voice._capture_audio_window", return_value=fake_audio), \
                 patch("voice._transcribe_wake_audio", side_effect=[None, "jarvis"]), \
                 patch("voice._debug_log"):
                voice.wait_for_wake_word()
        finally:
            voice._stop_requested.clear()

        self.assertEqual(events, ["enter", "exit", "enter", "exit"])

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
