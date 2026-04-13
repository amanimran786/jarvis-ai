import os
import tempfile
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import speech_recognition as sr
from openai import OpenAI
from config import (
    OPENAI_API_KEY,
    ELEVENLABS_API_KEY,
    ELEVENLABS_VOICE_ID,
    ELEVENLABS_MODEL,
    OPENAI_TTS_MODEL,
    OPENAI_TTS_VOICE,
    TTS_BACKENDS,
)
import call_privacy
from local_runtime import local_stt
from local_runtime import local_tts
# Use subprocess bridge so frozen .app doesn't need kokoro-onnx bundled
try:
    from local_runtime import local_kokoro_subprocess_tts as local_kokoro_tts
except ImportError:
    from local_runtime import local_kokoro_tts

_openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
_recognizer = sr.Recognizer()

WAKE_WORDS = {"jarvis", "hey jarvis", "ok jarvis", "okay jarvis"}
_last_tts_engine = ""
MANUAL_PROMPT_WINDOW_SECONDS = 8.0
WAKE_WORD_WINDOW_SECONDS = 3.0
_kokoro_disabled_reason = ""

# Preferred real microphones — avoid BlackHole (loopback bus, no physical mic)
_PREFERRED_MICS = ["MacBook Pro Microphone", "AirPods Pro", "iPhone Microphone", "Built-in Microphone"]
_BLACKHOLE_SKIP = ["blackhole", "loopback", "virtual"]
_VOICE_LOG_PATH = Path.home() / "Library" / "Application Support" / "Jarvis" / ".jarvis_voice.log"


def _debug_log(*args, **kwargs) -> None:
    """Best-effort logging that stays safe inside windowed macOS app bundles."""
    try:
        print(*args, **kwargs)
    except (BrokenPipeError, OSError, ValueError):
        pass
    try:
        _VOICE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = " ".join(str(arg) for arg in args)
        with _VOICE_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now().isoformat(timespec='seconds')}] {line}\n")
    except Exception:
        pass


def _get_microphone() -> sr.Microphone:
    """Return a Microphone on a real input device, skipping virtual loopback buses."""
    try:
        names = sr.Microphone.list_microphone_names()
        for preferred in _PREFERRED_MICS:
            for i, name in enumerate(names):
                if preferred.lower() in name.lower():
                    return sr.Microphone(device_index=i)
        for i, name in enumerate(names):
            if not any(skip in name.lower() for skip in _BLACKHOLE_SKIP):
                return sr.Microphone(device_index=i)
    except Exception:
        pass
    return sr.Microphone()


def _microphone_candidates() -> list[tuple[str, sr.Microphone]]:
    """Return microphone candidates ordered by preference.

    Priority:
      1. Exact or substring match against _PREFERRED_MICS (highest priority first).
      2. Fuzzy word match: any single significant word from a preferred name appears
         in the device name (handles e.g. "AirPods" matching "AirPods Pro Mic").
      3. Any real non-virtual device not in _BLACKHOLE_SKIP.
      4. macOS default input device as last resort.

    This ensures the Default input device is only used when nothing better exists,
    rather than always winning by being inserted first.
    """
    candidates: list[tuple[str, sr.Microphone]] = []
    seen: set[int | None] = set()

    try:
        names = sr.Microphone.list_microphone_names() or []
    except Exception:
        names = []

    def _append(index: int | None, label: str) -> None:
        if index in seen:
            return
        seen.add(index)
        candidates.append((label, sr.Microphone(device_index=index)))

    # Tier 1: substring match — "MacBook Pro Microphone" in device name
    for preferred in _PREFERRED_MICS:
        for i, name in enumerate(names):
            if preferred.lower() in name.lower():
                _append(i, name)

    # Tier 2: fuzzy word match — any word from the preferred name appears in
    # the device name (handles slight naming differences like "AirPods" vs "AirPods Pro Mic")
    _FUZZY_SKIP_WORDS = {"microphone", "mic", "the", "a", "an", "input", "output"}
    for preferred in _PREFERRED_MICS:
        words = [w for w in preferred.lower().split() if w not in _FUZZY_SKIP_WORDS and len(w) > 2]
        for i, name in enumerate(names):
            name_lower = name.lower()
            if any(word in name_lower for word in words):
                _append(i, name)

    # Tier 3: any real non-virtual device
    for i, name in enumerate(names):
        if not any(skip in name.lower() for skip in _BLACKHOLE_SKIP):
            _append(i, name)

    # Tier 4: macOS default input (last resort — may be a loopback or virtual device)
    _append(None, "Default input device")

    return candidates


@contextmanager
def _open_microphone_source():
    """Open a live microphone stream, skipping candidates that fail to provide one."""
    last_error: Exception | None = None

    for label, microphone in _microphone_candidates():
        source = None
        try:
            source = microphone.__enter__()
            if getattr(source, "stream", None) is None:
                raise RuntimeError(f"{label} opened without a live input stream")
            _debug_log(f"[Mic] Using input device: {label}")
            try:
                yield source
            finally:
                microphone.__exit__(None, None, None)
            return
        except Exception as exc:
            last_error = exc
            _debug_log(f"[Mic] Failed to open {label}: {exc}")
            try:
                if source is not None:
                    stream = getattr(source, "stream", None)
                    audio = getattr(source, "audio", None)
                    if stream is not None:
                        stream.close()
                    if audio is not None:
                        audio.terminate()
                    source.stream = None
            except Exception:
                pass

    detail = str(last_error) if last_error is not None else "No microphone devices are available."
    raise RuntimeError(f"Jarvis could not open a usable microphone input. {detail}")


def _capture_audio_window(source, *, duration: float, reason: str):
    """
    Record a fixed window of audio instead of waiting for speech_recognition's
    phrase gate to decide that the user started talking.
    """
    _debug_log(f"[Mic] Recording {duration:.1f}s audio window for {reason}.")
    return _recognizer.record(source, duration=duration)

# Prevents mic from picking up Jarvis's own TTS output.
# Cleared while Jarvis is speaking; listen() blocks until set again.
_done_speaking = threading.Event()
_done_speaking.set()  # initially not speaking
_stop_requested = threading.Event()
_manual_wake_trigger = threading.Event()  # set by UI to skip wake-word wait

# ── Ambient noise calibration cache ──────────────────────────────────────────
# Calibrate once per session and cache the energy threshold.
# adjust_for_ambient_noise() costs 300ms per call — we call it once and reuse.
import time as _time
_CALIBRATION_LOCK = threading.Lock()
_CALIBRATION_TTL = 12.0           # re-calibrate every 12 s — fresh after each TTS turn
_calibrated_at: float = 0.0
_calibrated_threshold: float | None = None


def _ensure_calibrated(source) -> None:
    """Calibrate ambient noise once; reuse the threshold until TTL expires."""
    global _calibrated_at, _calibrated_threshold
    now = _time.monotonic()
    if _calibrated_threshold is not None and (now - _calibrated_at) < _CALIBRATION_TTL:
        _recognizer.energy_threshold = _calibrated_threshold
        return
    with _CALIBRATION_LOCK:
        # Double-check after acquiring lock
        now = _time.monotonic()
        if _calibrated_threshold is not None and (now - _calibrated_at) < _CALIBRATION_TTL:
            _recognizer.energy_threshold = _calibrated_threshold
            return
        _recognizer.adjust_for_ambient_noise(source, duration=0.3)
        _calibrated_threshold = _recognizer.energy_threshold
        _calibrated_at = _time.monotonic()


def invalidate_noise_calibration() -> None:
    """Force re-calibration on the next listen() call (e.g., after environment change)."""
    global _calibrated_threshold
    _calibrated_threshold = None


def request_stop() -> None:
    _stop_requested.set()
    _done_speaking.set()


def clear_stop_request() -> None:
    _stop_requested.clear()

# ── ElevenLabs setup ──────────────────────────���───────────────────────────────

_eleven_client = None

def _get_eleven():
    global _eleven_client
    if _eleven_client is not None:
        return _eleven_client
    if not ELEVENLABS_API_KEY:
        return None
    try:
        from elevenlabs.client import ElevenLabs
        _eleven_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)
        return _eleven_client
    except ImportError:
        return None


def _speak_elevenlabs(text: str) -> bool:
    """Try to speak via ElevenLabs. Returns True on success."""
    client = _get_eleven()
    if not client:
        return False
    try:
        audio_gen = client.text_to_speech.convert(
            voice_id=ELEVENLABS_VOICE_ID,
            text=text,
            model_id=ELEVENLABS_MODEL,
            voice_settings={
                "stability": 0.45,
                "similarity_boost": 0.80,
                "style": 0.15,
                "use_speaker_boost": True,
            }
        )
        # Collect bytes from generator
        audio_bytes = b"".join(audio_gen) if hasattr(audio_gen, "__iter__") else audio_gen
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(audio_bytes)
            tmp_path = f.name
        subprocess.run(["afplay", tmp_path], check=True, capture_output=True)
        os.unlink(tmp_path)
        return True
    except Exception as e:
        _debug_log(f"[ElevenLabs] Failed: {e} — falling back to OpenAI TTS")
        return False


def _speak_openai(text: str) -> bool:
    """Speak via OpenAI TTS (fallback)."""
    if _openai_client is None:
        return False
    response = _openai_client.audio.speech.create(
        model=OPENAI_TTS_MODEL,
        voice=OPENAI_TTS_VOICE,
        input=text
    )
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(response.content)
        tmp_path = f.name
    try:
        subprocess.run(["afplay", tmp_path], check=True, capture_output=True)
        return True
    finally:
        os.unlink(tmp_path)


def _speak_local(text: str) -> bool:
    result = local_tts.speak(text)
    if not result.get("ok"):
        error = result.get("error")
        if error:
            _debug_log(f"[Local TTS] {error}")
        return False
    return True


def _speak_kokoro(text: str) -> bool:
    global _kokoro_disabled_reason
    if _kokoro_disabled_reason:
        return False
    result = local_kokoro_tts.speak(text)
    if not result.get("ok"):
        error = result.get("error")
        if error:
            normalized = error.lower()
            if any(
                phrase in normalized
                for phrase in (
                    "kokoro-onnx not installed",
                    "kokoro model unavailable",
                    "no module named",
                    "failed to load model",
                )
            ):
                _kokoro_disabled_reason = error
                _debug_log(f"[Kokoro TTS] disabling Kokoro for this session: {error}")
            else:
                _debug_log(f"[Kokoro TTS] {error}")
        return False
    return True


def _speak_with_fallbacks(text: str) -> str:
    global _last_tts_engine
    for backend in TTS_BACKENDS:
        try:
            if backend == "kokoro" and _speak_kokoro(text):
                _last_tts_engine = "Kokoro TTS"
                return _last_tts_engine
            if backend == "say" and _speak_local(text):
                _last_tts_engine = "Local TTS (say)"
                return _last_tts_engine
            if backend == "elevenlabs" and _speak_elevenlabs(text):
                _last_tts_engine = "ElevenLabs"
                return _last_tts_engine
            if backend == "openai" and _speak_openai(text):
                _last_tts_engine = "OpenAI TTS"
                return _last_tts_engine
        except Exception as exc:
            _debug_log(f"[TTS] Backend {backend} failed: {exc}")
    raise RuntimeError("No TTS backend succeeded.")


# ── Public speak API ─────────────────────────��────────────────────────────────

def speak(text: str) -> None:
    """Speak text — local macOS TTS first, then paid fallbacks if needed."""
    if not text or not text.strip():
        return
    _debug_log(f"Jarvis: {text}")
    if call_privacy.should_suppress_audio():
        _debug_log("[Voice] Suppressed audio because meeting-safe mode is active.")
        return
    _done_speaking.clear()
    try:
        _speak_with_fallbacks(text)
    finally:
        # Invalidate the noise calibration so the next listen() recalibrates
        # in post-speech silence rather than reusing a threshold measured while
        # the room was loud (which would suppress the user's voice).
        invalidate_noise_calibration()
        _done_speaking.set()


def _transcribe_wav_bytes(wav_bytes: bytes) -> str | None:
    """Transcribe WAV bytes in memory — no disk I/O."""
    local_result = local_stt.transcribe_audio(wav_bytes, language="en")
    if local_result.get("ok"):
        text = (local_result.get("text") or "").strip()
        if text:
            _debug_log(f"[STT] Transcribed locally via {local_result.get('engine')}.")
            return text

    local_error = local_result.get("error", "local transcription failed")
    if not local_stt.openai_fallback_allowed():
        _debug_log(f"[Local STT] {local_error}")
        return None
    if _openai_client is None:
        _debug_log(f"[Local STT] {local_error}")
        _debug_log("[Whisper Error] OpenAI STT fallback is not configured.")
        return None

    if local_result.get("engine") == "faster-whisper":
        _debug_log(f"[Local STT] {local_error} — falling back to OpenAI Whisper")

    try:
        import io
        audio_file = io.BytesIO(wav_bytes)
        audio_file.name = "audio.wav"
        transcript = _openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
        )
        return transcript.text.strip() or None
    except Exception as e:
        _debug_log(f"[Whisper Error] {e}")
        return None


def _transcribe_audio_file(path: str) -> str | None:
    """Transcribe from a file path — used by wake-word and legacy callers."""
    local_result = local_stt.transcribe_file(path, language="en")
    if local_result.get("ok"):
        text = (local_result.get("text") or "").strip()
        if text:
            _debug_log(f"[STT] Transcribed locally via {local_result.get('engine')}.")
            return text

    local_error = local_result.get("error", "local transcription failed")
    if not local_stt.openai_fallback_allowed():
        _debug_log(f"[Local STT] {local_error}")
        return None
    if _openai_client is None:
        _debug_log(f"[Local STT] {local_error}")
        _debug_log("[Whisper Error] OpenAI STT fallback is not configured.")
        return None

    if local_result.get("engine") == "faster-whisper":
        _debug_log(f"[Local STT] {local_error} — falling back to OpenAI Whisper")

    try:
        with open(path, "rb") as audio_file:
            transcript = _openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file
            )
        return transcript.text.strip() or None
    except Exception as e:
        _debug_log(f"[Whisper Error] {e}")
        return None


def speak_stream(text_chunks, *, on_text=None) -> str:
    """
    Speak a streaming response sentence by sentence.
    Plays each sentence as soon as it's complete while the next is generating.
    Returns the full response text.

    on_text: optional callable(accumulated_text: str) called after each sentence
             is spoken — lets callers update a live UI bubble in real time.
    """
    buffer = ""
    full_text = ""
    sentence_enders = {".", "!", "?"}

    if call_privacy.should_suppress_audio():
        for chunk in text_chunks:
            full_text += chunk
        if full_text.strip():
            _debug_log(f"Jarvis: {full_text}")
            _debug_log("[Voice] Suppressed streaming audio because meeting-safe mode is active.")
            if on_text:
                on_text(full_text)
        return full_text

    for chunk in text_chunks:
        buffer += chunk
        full_text += chunk

        while True:
            end_idx = -1
            for i, ch in enumerate(buffer):
                if ch in sentence_enders:
                    # Don't split on decimals like "3.14"
                    if ch == "." and 0 < i < len(buffer) - 1:
                        if buffer[i-1].isdigit() and buffer[i+1].isdigit():
                            continue
                    end_idx = i
                    break

            if end_idx == -1:
                break

            sentence = buffer[:end_idx + 1].strip()
            buffer = buffer[end_idx + 1:]
            if sentence:
                speak(sentence)
                if on_text:
                    on_text(full_text)

    if buffer.strip():
        speak(buffer.strip())
        if on_text:
            on_text(full_text)

    return full_text


# ── Listen / STT ────────────────────────────���─────────────────────────────────

def listen() -> str | None:
    """Record audio and transcribe with local faster-whisper when available."""
    if _stop_requested.is_set():
        return None
    _done_speaking.wait(timeout=30)
    if _stop_requested.is_set():
        return None

    # Unconditional pause — lets the mic stream initialise and any TTS room
    # echo die away before calibration samples ambient noise.  Without this,
    # calibration runs against reverb and sets the threshold too high,
    # silencing the user's voice every time.  The original code always had a
    # 0.3 s sleep here; restoring that behaviour.
    _time.sleep(0.3)

    try:
        with _open_microphone_source() as source:
            _debug_log("Listening...")
            _ensure_calibrated(source)
            audio = _capture_audio_window(
                source,
                duration=MANUAL_PROMPT_WINDOW_SECONDS,
                reason="manual prompt",
            )
    except RuntimeError as exc:
        _debug_log(f"[Mic] {exc}")
        return None

    # Transcribe in memory — no temp file write/read
    wav_bytes = audio.get_wav_data()
    text = _transcribe_wav_bytes(wav_bytes)
    if text:
        _debug_log(f"You: {text}")
    return text or None


def _wake_word_match(text: str) -> bool:
    normalized = " ".join((text or "").lower().split()).strip()
    if not normalized:
        return False
    return any(
        normalized == wake
        or normalized.startswith(wake + " ")
        or normalized.endswith(" " + wake)
        for wake in WAKE_WORDS
    )


def _transcribe_wake_audio(audio) -> str | None:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(audio.get_wav_data())
        tmp_path = f.name

    try:
        local_result = local_stt.transcribe_file(tmp_path, language="en")
        text = (local_result.get("text") or "").strip().lower()
        if text:
            return text
        local_error = (local_result.get("error") or "").strip()
        if local_error:
            _debug_log(f"[Wake STT] {local_error}")
    finally:
        os.unlink(tmp_path)

    try:
        import model_router

        allow_remote_fallback = not model_router.is_open_source_mode()
    except Exception:
        allow_remote_fallback = True

    if not allow_remote_fallback:
        return None

    try:
        return _recognizer.recognize_google(audio).lower().strip()
    except (sr.UnknownValueError, sr.RequestError):
        return None


def trigger_wake_word() -> None:
    """Manually trigger wake-word detection (skips waiting for the wake word)."""
    _manual_wake_trigger.set()


def wait_for_wake_word() -> None:
    """Listen for wake word using local STT first, with optional remote fallback."""
    # Honor a trigger that was set just before this call (e.g., mic button clicked
    # right as a new worker started). The trigger is always cleared after it fires.
    if _manual_wake_trigger.is_set():
        _manual_wake_trigger.clear()
        _debug_log("\n[Wake word manually triggered on entry]")
        return
    _debug_log("Waiting for wake word ('Hey Jarvis')... ", end="", flush=True)
    while True:
        if _stop_requested.is_set():
            return
        if _manual_wake_trigger.is_set():
            _manual_wake_trigger.clear()
            _debug_log("\n[Wake word manually triggered]")
            return
        # Also wait here if Jarvis is speaking
        _done_speaking.wait(timeout=10)
        if _stop_requested.is_set():
            return
        if _manual_wake_trigger.is_set():
            _manual_wake_trigger.clear()
            _debug_log("\n[Wake word manually triggered]")
            return
        try:
            with _open_microphone_source() as source:
                _ensure_calibrated(source)
                audio = _capture_audio_window(
                    source,
                    duration=WAKE_WORD_WINDOW_SECONDS,
                    reason="wake word",
                )
        except RuntimeError as exc:
            _debug_log(f"[Mic] {exc}")
            _time.sleep(0.5)
            continue

        text = _transcribe_wake_audio(audio)
        if _wake_word_match(text or ""):
            _debug_log(f"\n[Wake word detected: '{text}']")
            return
        _debug_log(".", end="", flush=True)


def tts_engine() -> str:
    """Return which TTS engine is active."""
    if _last_tts_engine:
        return _last_tts_engine
    if "say" in TTS_BACKENDS and local_tts.status().get("ready"):
        return "Local TTS (say)"
    if "elevenlabs" in TTS_BACKENDS and _get_eleven():
        return "ElevenLabs"
    if "openai" in TTS_BACKENDS and _openai_client is not None:
        return "OpenAI TTS"
    return "Unavailable"


def stt_engine() -> str:
    """Return which STT engine is active right now."""
    snap = local_stt.status()
    return snap.get("active_engine", "openai")
