from __future__ import annotations

import os
import threading
from typing import Any

from config import (
    OPENAI_API_KEY,
    OPENAI_STT_FALLBACK_ENABLED,
    LOCAL_STT_ENGINE,
    LOCAL_STT_LANGUAGE,
    FASTER_WHISPER_MODEL as LOCAL_STT_MODEL,
    FASTER_WHISPER_DEVICE,
    FASTER_WHISPER_COMPUTE_TYPE,
    FASTER_WHISPER_CPU_THREADS,
    FASTER_WHISPER_NUM_WORKERS,
    FASTER_WHISPER_BEAM_SIZE,
    FASTER_WHISPER_VAD_FILTER,
)


_MODEL_LOCK = threading.Lock()
_MODEL = None
_IMPORT_ERROR: str = ""


def _missing_vad_asset_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "silero_vad_v6.onnx" in text or ("no_suchfile" in text and "vad" in text)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def configured_engine() -> str:
    engine = (LOCAL_STT_ENGINE or "auto").strip().lower()
    if engine in {"local", "faster-whisper", "faster_whisper"}:
        return "faster-whisper"
    if engine == "openai":
        return "openai"
    return "auto"


def local_available() -> bool:
    global _IMPORT_ERROR
    try:
        import faster_whisper  # noqa: F401
        _IMPORT_ERROR = ""
        return True
    except Exception as exc:
        _IMPORT_ERROR = str(exc)
        return False


def status() -> dict[str, Any]:
    engine = configured_engine()
    available = local_available()
    openai_allowed = openai_fallback_allowed()
    active = "faster-whisper" if (engine != "openai" and available) else ("openai" if openai_allowed else "unavailable")
    return {
        "configured_engine": engine,
        "active_engine": active,
        "local_available": available,
        "openai_fallback_allowed": openai_allowed,
        "model": LOCAL_STT_MODEL,
        "device": FASTER_WHISPER_DEVICE,
        "compute_type": FASTER_WHISPER_COMPUTE_TYPE,
        "language": LOCAL_STT_LANGUAGE,
        "import_error": _IMPORT_ERROR,
    }


def openai_fallback_allowed() -> bool:
    if not OPENAI_STT_FALLBACK_ENABLED or not OPENAI_API_KEY:
        return False
    try:
        import model_router
        return not model_router.is_open_source_mode()
    except Exception:
        return True


def _get_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL

    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL

        from faster_whisper import WhisperModel

        _MODEL = WhisperModel(
            LOCAL_STT_MODEL,
            device=FASTER_WHISPER_DEVICE,
            compute_type=FASTER_WHISPER_COMPUTE_TYPE,
            cpu_threads=FASTER_WHISPER_CPU_THREADS,
            num_workers=FASTER_WHISPER_NUM_WORKERS,
        )
        return _MODEL


def _wav_bytes_to_numpy(wav_bytes: bytes):
    """Convert WAV bytes to a float32 numpy array at 16kHz — no disk I/O."""
    import io
    import wave
    import numpy as np

    with io.BytesIO(wav_bytes) as buf:
        with wave.open(buf, "rb") as wf:
            n_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            frame_rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())

    dtype = np.int16 if sample_width == 2 else np.int8
    samples = np.frombuffer(raw, dtype=dtype).astype(np.float32)

    # Mix down to mono if stereo
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1)

    # Resample to 16kHz if needed (faster-whisper expects 16kHz).
    # Use scipy for proper anti-aliased resampling (avoids aliasing from
    # 44.1kHz/48kHz mic capture that degrades Whisper accuracy).
    # Falls back to nearest-neighbor if scipy is unavailable.
    if frame_rate != 16000:
        try:
            from scipy.signal import resample as scipy_resample
            new_len = int(len(samples) * 16000 / frame_rate)
            samples = scipy_resample(samples, new_len).astype(np.float32)
        except ImportError:
            ratio = 16000 / frame_rate
            new_len = int(len(samples) * ratio)
            indices = np.round(np.linspace(0, len(samples) - 1, new_len)).astype(np.int32)
            samples = samples[indices]

    # Normalize to [-1, 1]
    max_val = 32768.0 if sample_width == 2 else 128.0
    return samples / max_val


def _run_transcription(audio_input, *, language: str | None = None) -> dict[str, Any]:
    """Core transcription — accepts file path or numpy float32 array."""
    model = _get_model()
    primary_kwargs = {
        "language": language or LOCAL_STT_LANGUAGE or None,
        "beam_size": FASTER_WHISPER_BEAM_SIZE,
        "vad_filter": FASTER_WHISPER_VAD_FILTER,
        "condition_on_previous_text": False,
        # Suppress hallucinations and low-confidence outputs
        "no_speech_threshold": 0.4,
        "compression_ratio_threshold": 2.4,
        "log_prob_threshold": -1.0,
        "hallucination_silence_threshold": 0.3,
    }

    def _transcribe_with_kwargs(kwargs: dict[str, Any]):
        try:
            return model.transcribe(audio_input, **kwargs)
        except Exception as exc:
            if not kwargs["vad_filter"] or not _missing_vad_asset_error(exc):
                raise
            fallback_kwargs = dict(kwargs)
            fallback_kwargs["vad_filter"] = False
            return model.transcribe(audio_input, **fallback_kwargs)

    segments, info = _transcribe_with_kwargs(primary_kwargs)
    text = " ".join(segment.text.strip() for segment in segments if segment.text).strip()
    if not text and primary_kwargs["vad_filter"]:
        retry_kwargs = dict(primary_kwargs)
        retry_kwargs.update(
            {
                "vad_filter": False,
                "no_speech_threshold": 0.2,
                "hallucination_silence_threshold": 0.1,
            }
        )
        segments, info = _transcribe_with_kwargs(retry_kwargs)
        text = " ".join(segment.text.strip() for segment in segments if segment.text).strip()

    return {
        "ok": bool(text),
        "engine": "faster-whisper",
        "text": text,
        "language": getattr(info, "language", LOCAL_STT_LANGUAGE),
        "error": "" if text else "empty transcript",
    }


def transcribe_audio(wav_bytes: bytes, *, language: str | None = None) -> dict[str, Any]:
    """
    Transcribe raw WAV bytes in memory — zero disk I/O.
    Preferred over transcribe_file() when the caller already has WAV bytes.
    """
    engine = configured_engine()
    if engine == "openai":
        return {"ok": False, "engine": "openai", "text": "", "error": "local STT disabled"}

    if not local_available():
        return {"ok": False, "engine": "faster-whisper", "text": "", "error": _IMPORT_ERROR or "faster-whisper not installed"}

    try:
        audio_array = _wav_bytes_to_numpy(wav_bytes)
        return _run_transcription(audio_array, language=language)
    except Exception as exc:
        return {"ok": False, "engine": "faster-whisper", "text": "", "error": str(exc)}


def transcribe_file(path: str, *, language: str | None = None) -> dict[str, Any]:
    """
    Attempt local transcription via faster-whisper.
    Returns a structured result and leaves fallback policy to the caller.
    """
    engine = configured_engine()
    if engine == "openai":
        return {
            "ok": False,
            "engine": "openai",
            "text": "",
            "error": "local STT disabled by configuration",
        }

    if not local_available():
        return {
            "ok": False,
            "engine": "faster-whisper",
            "text": "",
            "error": _IMPORT_ERROR or "faster-whisper is not installed",
        }

    try:
        return _run_transcription(path, language=language)
    except Exception as exc:
        return {
            "ok": False,
            "engine": "faster-whisper",
            "text": "",
            "error": str(exc),
        }


def preload() -> None:
    """
    Load the faster-whisper model into memory in a background thread.
    Call at startup so the first real transcription has zero cold-start latency.
    """
    if not local_available():
        return

    def _load():
        try:
            _get_model()
            print("[Local STT] Model pre-loaded and ready.")
        except Exception as e:
            print(f"[Local STT] Preload failed (non-fatal): {e}")

    t = threading.Thread(target=_load, daemon=True, name="LocalSTTPreload")
    t.start()
