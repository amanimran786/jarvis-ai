import unittest
from types import SimpleNamespace
from unittest.mock import patch

from local_runtime import local_stt


class LocalSttRuntimeTests(unittest.TestCase):
    def test_status_reports_faster_whisper_device_and_compute_type(self):
        with patch("local_runtime.local_stt.configured_engine", return_value="faster-whisper"), \
             patch("local_runtime.local_stt.local_available", return_value=True), \
             patch("local_runtime.local_stt.openai_fallback_allowed", return_value=False), \
             patch("local_runtime.local_stt.LOCAL_STT_MODEL", "base.en"), \
             patch("local_runtime.local_stt.FASTER_WHISPER_DEVICE", "cpu"), \
             patch("local_runtime.local_stt.FASTER_WHISPER_COMPUTE_TYPE", "int8"):
            result = local_stt.status()

        self.assertEqual(result["active_engine"], "faster-whisper")
        self.assertEqual(result["device"], "cpu")
        self.assertEqual(result["compute_type"], "int8")

    def test_run_transcription_retries_without_vad_when_vad_asset_is_missing(self):
        fake_model = SimpleNamespace()
        calls = []

        def _transcribe(audio_input, **kwargs):
            calls.append(kwargs.copy())
            if len(calls) == 1:
                raise RuntimeError(
                    "[ONNXRuntimeError] : 3 : NO_SUCHFILE : Load model from "
                    "/tmp/faster_whisper/assets/silero_vad_v6.onnx failed"
                )
            return ([SimpleNamespace(text="hello world")], SimpleNamespace(language="en"))

        fake_model.transcribe = _transcribe

        with patch("local_runtime.local_stt._get_model", return_value=fake_model), \
             patch("local_runtime.local_stt.FASTER_WHISPER_VAD_FILTER", True):
            result = local_stt._run_transcription("audio.wav", language="en")

        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "hello world")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["vad_filter"])
        self.assertFalse(calls[1]["vad_filter"])

    def test_run_transcription_retries_empty_result_with_looser_thresholds(self):
        fake_model = SimpleNamespace()
        calls = []

        def _transcribe(audio_input, **kwargs):
            calls.append(kwargs.copy())
            if len(calls) == 1:
                return ([SimpleNamespace(text="")], SimpleNamespace(language="en"))
            return ([SimpleNamespace(text="hello again")], SimpleNamespace(language="en"))

        fake_model.transcribe = _transcribe

        with patch("local_runtime.local_stt._get_model", return_value=fake_model), \
             patch("local_runtime.local_stt.FASTER_WHISPER_VAD_FILTER", True):
            result = local_stt._run_transcription("audio.wav", language="en")

        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "hello again")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["vad_filter"])
        self.assertFalse(calls[1]["vad_filter"])
        self.assertEqual(calls[1]["no_speech_threshold"], 0.2)
        self.assertEqual(calls[1]["hallucination_silence_threshold"], 0.1)


if __name__ == "__main__":
    unittest.main()
