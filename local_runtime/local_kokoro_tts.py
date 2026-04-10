from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from config import JARVIS_KOKORO_VOICE, JARVIS_KOKORO_TTS_ENABLED

DEFAULT_ENGINE = "kokoro"
SAMPLE_RATE = 24000
MAX_TEXT_LENGTH = 4000

# Model files live in ~/.jarvis/kokoro/ for permanent caching
_MODEL_DIR = Path.home() / ".jarvis" / "kokoro"
_MODEL_PATH = _MODEL_DIR / "kokoro-v1.0.onnx"
_VOICES_PATH = _MODEL_DIR / "voices-v1.0.bin"

_MODEL_URLS = {
    "model": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
    "voices": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}

_kokoro_lock = threading.Lock()
_kokoro_instance = None

# ── Phrase cache ──────────────────────────────────────────────────────────────
# Common short acknowledgement phrases pre-rendered to WAV bytes at startup.
# Cache hit = zero ONNX inference — afplay fires immediately.
_CACHED_PHRASES = [
    "Yes?", "Got it.", "Alright.", "Sure.", "Done.",
    "On it.", "Opening that now.", "Setting that up.",
    "I'm listening.", "Still here.", "Goodbye.",
    "Restarting now.", "One moment.", "Let me check.",
    "Got it, setting a timer.", "Timer set.", "Timer started.",
    "Good morning.", "Online.", "Ready.",
]
_phrase_cache: dict[str, bytes] = {}   # normalised text → WAV bytes
_phrase_cache_lock = threading.Lock()


def _cache_key(text: str) -> str:
    return " ".join(text.strip().lower().split())


def prewarm_phrase_cache() -> None:
    """Render all cached phrases in a background thread at startup."""
    def _render():
        model = _get_model()
        if model is None:
            return
        voice = JARVIS_KOKORO_VOICE
        for phrase in _CACHED_PHRASES:
            key = _cache_key(phrase)
            if key in _phrase_cache:
                continue
            try:
                samples, sr = model.create(phrase, voice=voice, speed=1.0, lang="en-us")
                wav = _samples_to_wav_bytes(samples, sr)
                with _phrase_cache_lock:
                    _phrase_cache[key] = wav
            except Exception:
                pass

    threading.Thread(target=_render, daemon=True, name="KokoroPhraseCache").start()


def _samples_to_wav_bytes(samples, sample_rate: int) -> bytes:
    """Convert numpy float32 samples to WAV bytes in memory."""
    import io
    buf = io.BytesIO()
    try:
        import numpy as np
        import scipy.io.wavfile as wav_io
        samples_int16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
        wav_io.write(buf, sample_rate, samples_int16)
    except Exception:
        import wave
        import numpy as np
        samples_int16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(samples_int16.tobytes())
    return buf.getvalue()


def _kokoro_importable() -> bool:
    try:
        import kokoro_onnx  # noqa: F401
        return True
    except ImportError:
        return False


def _ensure_model_files() -> bool:
    if _MODEL_PATH.exists() and _VOICES_PATH.exists():
        return True
    _MODEL_DIR.mkdir(parents=True, exist_ok=True)
    try:
        import urllib.request

        needs_download = not _MODEL_PATH.exists() or not _VOICES_PATH.exists()
        if needs_download:
            print("[Kokoro] Downloading model files (~85MB)...")

        if not _MODEL_PATH.exists():
            print(f"[Kokoro] Saving model to {_MODEL_PATH} ...")
            urllib.request.urlretrieve(_MODEL_URLS["model"], str(_MODEL_PATH))
        if not _VOICES_PATH.exists():
            print(f"[Kokoro] Saving voices to {_VOICES_PATH} ...")
            urllib.request.urlretrieve(_MODEL_URLS["voices"], str(_VOICES_PATH))
        return True
    except Exception as exc:
        print(f"[Kokoro] Failed to download model files: {exc}")
        return False


def _get_model():
    global _kokoro_instance
    if _kokoro_instance is not None:
        return _kokoro_instance
    with _kokoro_lock:
        if _kokoro_instance is not None:
            return _kokoro_instance
        if not _ensure_model_files():
            return None
        try:
            from kokoro_onnx import Kokoro
            _kokoro_instance = Kokoro(str(_MODEL_PATH), str(_VOICES_PATH))
            return _kokoro_instance
        except Exception as exc:
            print(f"[Kokoro] Failed to load model: {exc}")
            return None


def available() -> bool:
    if not JARVIS_KOKORO_TTS_ENABLED:
        return False
    if not _kokoro_importable():
        return False
    return _MODEL_PATH.exists() and _VOICES_PATH.exists()


def config() -> dict[str, Any]:
    return {
        "engine": DEFAULT_ENGINE,
        "enabled": JARVIS_KOKORO_TTS_ENABLED,
        "voice": JARVIS_KOKORO_VOICE,
        "sample_rate": SAMPLE_RATE,
        "model_path": str(_MODEL_PATH),
        "voices_path": str(_VOICES_PATH),
        "available": available(),
        "max_text_length": MAX_TEXT_LENGTH,
    }


def _normalize_text(text: str) -> str:
    normalized = " ".join((text or "").split()).strip()
    if len(normalized) > MAX_TEXT_LENGTH:
        normalized = normalized[:MAX_TEXT_LENGTH].rstrip()
    return normalized


def _write_wav_scipy(samples, sample_rate: int, path: str) -> None:
    import numpy as np
    import scipy.io.wavfile as wav_io
    samples_int16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    wav_io.write(path, sample_rate, samples_int16)


def _write_wav_wave(samples, sample_rate: int, path: str) -> None:
    import wave
    import numpy as np
    samples_int16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit = 2 bytes
        wf.setframerate(sample_rate)
        wf.writeframes(samples_int16.tobytes())


def _write_wav(samples, sample_rate: int, path: str) -> None:
    """Write samples to WAV; falls back to wave module if scipy is unavailable."""
    try:
        _write_wav_scipy(samples, sample_rate, path)
    except Exception:
        _write_wav_wave(samples, sample_rate, path)


def _play_wav_bytes(wav_bytes: bytes) -> None:
    """Write WAV bytes to a temp file and play with afplay, then delete."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        tmp_path = f.name
    try:
        subprocess.run(["afplay", tmp_path], check=True, capture_output=True)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def speak(text: str) -> dict[str, Any]:
    normalized = _normalize_text(text)
    voice = JARVIS_KOKORO_VOICE

    if not normalized:
        return {"ok": False, "engine": DEFAULT_ENGINE, "spoken": False,
                "text": "", "voice": voice, "error": "empty text"}

    if not JARVIS_KOKORO_TTS_ENABLED:
        return {"ok": False, "engine": DEFAULT_ENGINE, "spoken": False,
                "text": normalized, "voice": voice, "error": "kokoro TTS disabled by configuration"}

    if not _kokoro_importable():
        return {"ok": False, "engine": DEFAULT_ENGINE, "spoken": False,
                "text": normalized, "voice": voice, "error": "kokoro-onnx not installed"}

    # ── Cache hit: play pre-rendered WAV instantly, skip ONNX inference ──────
    key = _cache_key(normalized)
    with _phrase_cache_lock:
        cached_wav = _phrase_cache.get(key)
    if cached_wav is not None:
        try:
            _play_wav_bytes(cached_wav)
            return {"ok": True, "engine": DEFAULT_ENGINE, "spoken": True,
                    "text": normalized, "voice": voice, "error": ""}
        except Exception as exc:
            pass  # fall through to fresh synthesis on playback failure

    model = _get_model()
    if model is None:
        return {"ok": False, "engine": DEFAULT_ENGINE, "spoken": False,
                "text": normalized, "voice": voice, "error": "kokoro model unavailable"}

    try:
        samples, sample_rate = model.create(normalized, voice=voice, speed=1.0, lang="en-us")
        wav_bytes = _samples_to_wav_bytes(samples, sample_rate)
        _play_wav_bytes(wav_bytes)
        return {"ok": True, "engine": DEFAULT_ENGINE, "spoken": True,
                "text": normalized, "voice": voice, "error": ""}
    except Exception as exc:
        return {"ok": False, "engine": DEFAULT_ENGINE, "spoken": False,
                "text": normalized, "voice": voice, "error": str(exc)}
