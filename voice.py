import os
import tempfile
import subprocess
import threading
import speech_recognition as sr
from openai import OpenAI
from config import OPENAI_API_KEY, ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, ELEVENLABS_MODEL

_openai_client = OpenAI(api_key=OPENAI_API_KEY)
_recognizer = sr.Recognizer()

WAKE_WORDS = {"hey jarvis", "ok jarvis"}
OPENAI_TTS_VOICE = "onyx"   # fallback voice

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


def _speak_openai(text: str):
    """Speak via OpenAI TTS (fallback)."""
    response = _openai_client.audio.speech.create(
        model="tts-1",
        voice=OPENAI_TTS_VOICE,
        input=text
    )
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(response.content)
        tmp_path = f.name
    subprocess.run(["afplay", tmp_path], check=True, capture_output=True)
    os.unlink(tmp_path)


# ── Public speak API ─────────────────────────��────────────────────────────────

def speak(text: str) -> None:
    """Speak text — ElevenLabs first, OpenAI TTS as fallback."""
    if not text or not text.strip():
        return
    print(f"Jarvis: {text}")
    _done_speaking.clear()
    try:
        if not _speak_elevenlabs(text):
            _speak_openai(text)
    finally:
        _done_speaking.set()


def speak_stream(text_chunks) -> str:
    """
    Speak a streaming response sentence by sentence.
    Plays each sentence as soon as it's complete while the next is generating.
    Returns the full response text.
    """
    buffer = ""
    full_text = ""
    sentence_enders = {".", "!", "?"}

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
    """Record audio and transcribe with OpenAI Whisper."""
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
        with open(tmp_path, "rb") as audio_file:
            transcript = _openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file
            )
        text = transcript.text.strip()
        if text:
            print(f"You: {text}")
        return text or None
    except Exception as e:
        print(f"[Whisper Error] {e}")
        return None
    finally:
        os.unlink(tmp_path)


def wait_for_wake_word() -> None:
    """Listen for wake word using Google STT (fast, free, low-latency)."""
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

        try:
            text = _recognizer.recognize_google(audio).lower().strip()
            if any(text == w or text.startswith(w + " ") or text.endswith(" " + w) for w in WAKE_WORDS):
                print(f"\n[Wake word detected: '{text}']")
                return
        except (sr.UnknownValueError, sr.RequestError):
            pass


def tts_engine() -> str:
    """Return which TTS engine is active."""
    return "ElevenLabs" if _get_eleven() else "OpenAI TTS"
