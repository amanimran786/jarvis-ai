import os
import tempfile
import subprocess
import threading
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

_openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
_recognizer = sr.Recognizer()

WAKE_WORDS = {"hey jarvis", "ok jarvis"}
_last_tts_engine = ""

# Prevents mic from picking up Jarvis's own TTS output.
# Cleared while Jarvis is speaking; listen() blocks until set again.
_done_speaking = threading.Event()
_done_speaking.set()  # initially not speaking

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
        print(f"[ElevenLabs] Failed: {e} — falling back to OpenAI TTS")
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
            print(f"[Local TTS] {error}")
        return False
    return True


def _speak_with_fallbacks(text: str) -> str:
    global _last_tts_engine
    for backend in TTS_BACKENDS:
        try:
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
            print(f"[TTS] Backend {backend} failed: {exc}")
    raise RuntimeError("No TTS backend succeeded.")


# ── Public speak API ─────────────────────────��────────────────────────────────

def speak(text: str) -> None:
    """Speak text — local macOS TTS first, then paid fallbacks if needed."""
    if not text or not text.strip():
        return
    print(f"Jarvis: {text}")
    if call_privacy.should_suppress_audio():
        print("[Voice] Suppressed audio because meeting-safe mode is active.")
        return
    _done_speaking.clear()
    try:
        _speak_with_fallbacks(text)
    finally:
        _done_speaking.set()


def _transcribe_audio_file(path: str) -> str | None:
    local_result = local_stt.transcribe_file(path, language="en")
    if local_result.get("ok"):
        text = (local_result.get("text") or "").strip()
        if text:
            print(f"[STT] Transcribed locally via {local_result.get('engine')}.")
            return text

    local_error = local_result.get("error", "local transcription failed")
    if not local_stt.openai_fallback_allowed():
        print(f"[Local STT] {local_error}")
        return None
    if _openai_client is None:
        print(f"[Local STT] {local_error}")
        print("[Whisper Error] OpenAI STT fallback is not configured.")
        return None

    if local_result.get("engine") == "faster-whisper":
        print(f"[Local STT] {local_error} — falling back to OpenAI Whisper")

    try:
        with open(path, "rb") as audio_file:
            transcript = _openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file
            )
        return transcript.text.strip() or None
    except Exception as e:
        print(f"[Whisper Error] {e}")
        return None


def speak_stream(text_chunks) -> str:
    """
    Speak a streaming response sentence by sentence.
    Plays each sentence as soon as it's complete while the next is generating.
    Returns the full response text.
    """
    buffer = ""
    full_text = ""
    sentence_enders = {".", "!", "?"}

    if call_privacy.should_suppress_audio():
        for chunk in text_chunks:
            full_text += chunk
        if full_text.strip():
            print(f"Jarvis: {full_text}")
            print("[Voice] Suppressed streaming audio because meeting-safe mode is active.")
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

    if buffer.strip():
        speak(buffer.strip())

    return full_text


# ── Listen / STT ────────────────────────────���─────────────────────────────────

def listen() -> str | None:
    """Record audio and transcribe with local faster-whisper when available."""
    # Wait until Jarvis finishes speaking — prevents TTS feedback loop
    _done_speaking.wait(timeout=30)
    import time; time.sleep(0.3)

    with sr.Microphone() as source:
        print("Listening...")
        _recognizer.adjust_for_ambient_noise(source, duration=0.3)
        try:
            audio = _recognizer.listen(source, timeout=6, phrase_time_limit=60)
        except sr.WaitTimeoutError:
            return None

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(audio.get_wav_data())
        tmp_path = f.name

    try:
        text = _transcribe_audio_file(tmp_path)
        if text:
            print(f"You: {text}")
        return text or None
    finally:
        os.unlink(tmp_path)


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


def wait_for_wake_word() -> None:
    """Listen for wake word using local STT first, with optional remote fallback."""
    print("Waiting for wake word ('Hey Jarvis')... ", end="", flush=True)
    while True:
        # Also wait here if Jarvis is speaking
        _done_speaking.wait(timeout=10)
        with sr.Microphone() as source:
            _recognizer.adjust_for_ambient_noise(source, duration=0.2)
            try:
                audio = _recognizer.listen(source, timeout=3, phrase_time_limit=4)
            except sr.WaitTimeoutError:
                print(".", end="", flush=True)
                continue

        text = _transcribe_wake_audio(audio)
        if _wake_word_match(text or ""):
            print(f"\n[Wake word detected: '{text}']")
            return


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
