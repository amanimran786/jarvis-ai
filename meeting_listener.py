"""
Smart meeting listener for Jarvis.

Captures call audio in real-time via BlackHole or microphone,
transcribes with Whisper, and generates smart response suggestions
that appear only in the hidden Jarvis window.

Nobody on the call sees or hears any of this.
"""

import threading
import tempfile
import os
import time
import re
import numpy as np
import sounddevice as sd
import wave
from openai import OpenAI
from config import OPENAI_API_KEY
from provider_priority import ask_with_priority

client = OpenAI(api_key=OPENAI_API_KEY)

SAMPLE_RATE    = 16000
CHUNK_SECONDS  = 8     # transcribe every 8 seconds
OVERLAP_SECONDS = 2    # keep last 2s for continuity
CONTEXT_LIMIT  = 20    # keep last 20 transcript lines for context
MIC_RMS_THRESHOLD = 25
SYSTEM_AUDIO_RMS_THRESHOLD = 120

_running       = False
_thread        = None
_on_transcript = None   # callback(text: str)
_on_suggestion = None   # callback(text: str)
_device_index  = None   # None = default mic
_transcript_history = []
_actual_sample_rate = SAMPLE_RATE  # resolved at start()


def list_audio_devices() -> list[dict]:
    """Return all available audio input devices."""
    devices = []
    for i, d in enumerate(sd.query_devices()):
        if d['max_input_channels'] > 0:
            devices.append({"index": i, "name": d['name'], "channels": d['max_input_channels']})
    return devices


def get_blackhole_device() -> int | None:
    """Auto-detect BlackHole device index."""
    for d in list_audio_devices():
        if "blackhole" in d['name'].lower():
            return d['index']
    return None


def get_virtual_meeting_audio_device() -> int | None:
    """Prefer native meeting audio loopback devices when available."""
    meeting_label = None
    try:
        import overlay
        meeting_label = overlay.detect_meeting_app()
    except Exception:
        meeting_label = None

    meeting_specific_markers = {
        "TEAMS": ("microsoft teams audio",),
        "ZOOM": ("zoomaudio", "zoom audio"),
        "WEBEX": ("webex",),
    }
    generic_markers = ("loopback",)

    preferred_markers = meeting_specific_markers.get(meeting_label, ())
    if not preferred_markers:
        preferred_markers = generic_markers

    for d in list_audio_devices():
        name = d["name"].lower()
        if any(marker in name for marker in preferred_markers):
            return d["index"]
    if preferred_markers != generic_markers:
        for d in list_audio_devices():
            name = d["name"].lower()
            if any(marker in name for marker in generic_markers):
                return d["index"]
    return None


def get_preferred_microphone_device() -> int | None:
    """Prefer the built-in MacBook mic over continuity or app-specific devices."""
    devices = list_audio_devices()
    for d in devices:
        name = d["name"].lower()
        if "macbook" in name and "microphone" in name:
            return d["index"]
    for d in devices:
        name = d["name"].lower()
        if "microphone" in name and "iphone" not in name:
            return d["index"]
    return None


def set_device(index: int | None):
    """Set which audio input device to listen from."""
    global _device_index
    _device_index = index


def current_device_index() -> int | None:
    return _device_index


def _resolve_device_sample_rate(device_index) -> int:
    """Return the device's native sample rate to avoid PortAudio -50 errors."""
    try:
        idx = device_index if device_index is not None else sd.default.device[0]
        info = sd.query_devices(idx)
        return int(info['default_samplerate'])
    except Exception:
        return SAMPLE_RATE


def _device_name(device_index: int | None) -> str:
    try:
        idx = device_index if device_index is not None else sd.default.device[0]
        info = sd.query_devices(idx)
        return str(info["name"])
    except Exception:
        return "default input"


def preferred_source_snapshot() -> dict:
    blackhole = get_blackhole_device()
    if blackhole is not None:
        return {
            "kind": "system_audio",
            "device_index": blackhole,
            "device_name": _device_name(blackhole),
            "fallback": False,
        }

    meeting_audio = get_virtual_meeting_audio_device()
    if meeting_audio is not None:
        return {
            "kind": "meeting_audio",
            "device_index": meeting_audio,
            "device_name": _device_name(meeting_audio),
            "fallback": False,
        }

    mic = get_preferred_microphone_device()
    return {
        "kind": "microphone",
        "device_index": mic,
        "device_name": _device_name(mic),
        "fallback": True,
    }


def status_snapshot() -> dict:
    preferred = preferred_source_snapshot()
    active_index = _device_index if _device_index is not None else preferred["device_index"]
    return {
        "running": _running,
        "preferred": preferred,
        "active_device_index": active_index,
        "active_device_name": _device_name(active_index),
        "sample_rate": _actual_sample_rate,
    }


def _silence_threshold() -> int:
    name = _device_name(_device_index).lower()
    if "blackhole" in name or "teams audio" in name:
        return SYSTEM_AUDIO_RMS_THRESHOLD
    return MIC_RMS_THRESHOLD


def _record_chunk(seconds: int) -> np.ndarray:
    """Record a chunk of audio from the selected device."""
    global _actual_sample_rate
    frames = sd.rec(
        int(seconds * _actual_sample_rate),
        samplerate=_actual_sample_rate,
        channels=1,
        dtype='int16',
        device=_device_index
    )
    sd.wait()
    return frames.flatten()


def _save_wav(audio: np.ndarray) -> str:
    """Save audio to a temp WAV file at 16000 Hz for Whisper (resamples if needed)."""
    target_rate = 16000
    if _actual_sample_rate != target_rate:
        # Simple linear resample
        ratio = target_rate / _actual_sample_rate
        new_len = int(len(audio) * ratio)
        indices = np.linspace(0, len(audio) - 1, new_len)
        audio = np.interp(indices, np.arange(len(audio)), audio.astype(float)).astype(np.int16)

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    with wave.open(tmp.name, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(target_rate)
        wf.writeframes(audio.tobytes())
    return tmp.name


# Whisper hallucinates these patterns on silence/noise — discard them
_WHISPER_HALLUCINATIONS = {
    "thank you", "thanks for watching", "thanks for listening",
    "please subscribe", "subtitles by", "subs by", "www.",
    "transcribed by", "amara.org", ".co.uk", ".com",
}

def _is_hallucination(text: str) -> bool:
    low = text.lower()
    # All emoji / non-ASCII gibberish
    if all(not c.isascii() or not c.isalpha() for c in text.replace(" ", "")):
        return True
    # Known Whisper silence artifacts
    if any(h in low for h in _WHISPER_HALLUCINATIONS):
        return True
    # Too short to be real speech
    if len(text.split()) < 3:
        return True
    return False


def _transcribe(path: str) -> str:
    """Transcribe audio file with Whisper."""
    try:
        with open(path, 'rb') as f:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="en"
            )
        text = result.text.strip()
        if _is_hallucination(text):
            return ""
        return text
    except Exception as e:
        return ""
    finally:
        if os.path.exists(path):
            os.unlink(path)


def _generate_suggestion(new_line: str) -> str:
    """
    Given recent transcript context and a new line,
    generate a smart response suggestion.
    """
    if not new_line or len(new_line.split()) < 4:
        return ""  # too short to be meaningful

    context = "\n".join(_transcript_history[-10:])
    question_like = _looks_like_question(new_line)
    technical = _looks_technical(new_line + "\n" + context)
    tier = "strong" if (question_like or technical) else "cheap"
    mode_line = (
        "The latest line is a direct question. Answer it plainly and immediately."
        if question_like else
        "The latest line is not a direct question. Give the best concise response or follow-up."
    )

    prompt = f"""You are a real-time meeting assistant helping Aman respond during a live call.

Recent conversation transcript:
{context}

Latest thing just said:
"{new_line}"

Instructions:
- {mode_line}
- Return exactly what Aman should say next, not analysis about the conversation.
- Keep it clear, concise, and spoken naturally.
- Default to 1-2 sentences.
- If the question is technical, give the precise answer first and the key rationale second.
- If there is uncertainty, still give the best answer you can instead of hedging.
- Do not say "I suggest", "You could say", "Aman should say", or similar framing.
- Do not use bullets, headers, markdown, or filler.
- Do not invent personal experience that was not stated in the transcript."""

    try:
        return ask_with_priority(prompt, tier=tier)
    except Exception:
        return ""


def _looks_like_question(text: str) -> bool:
    low = (text or "").strip().lower()
    if "?" in low:
        return True
    starters = (
        "what", "why", "how", "when", "where", "which", "who",
        "can you", "could you", "would you", "do you", "did you",
        "is it", "are you", "tell me", "walk me through", "explain",
    )
    return any(low.startswith(prefix) for prefix in starters)


def _looks_technical(text: str) -> bool:
    low = (text or "").lower()
    markers = (
        "variable", "function", "class", "python", "javascript", "api", "sql",
        "database", "docker", "nginx", "index", "latency", "cache", "thread",
        "locking", "algorithm", "complexity", "kubernetes", "service", "backend",
        "frontend", "schema", "migration", "system design", "microservice",
    )
    return any(marker in low for marker in markers) or bool(re.search(r"\b[a-z_]+\(\)|\bo\(.*\)", low))


def _listen_loop():
    """Main listening loop — runs in background thread."""
    global _running, _transcript_history, _actual_sample_rate

    # Resolve device's native sample rate once before recording
    _actual_sample_rate = _resolve_device_sample_rate(_device_index)
    print(f"[SmartListen] Using sample rate: {_actual_sample_rate}Hz")

    # Import speaking guard from voice module
    try:
        from voice import _done_speaking as _speaking_guard
    except ImportError:
        _speaking_guard = None

    overlap_audio = np.array([], dtype='int16')
    print("[SmartListen] Listening to call audio...")

    while _running:
        try:
            # Don't record while Jarvis is speaking — avoids transcribing own TTS
            if _speaking_guard and not _speaking_guard.is_set():
                time.sleep(0.1)
                continue

            # Record chunk (with overlap from last chunk prepended)
            new_audio = _record_chunk(CHUNK_SECONDS - OVERLAP_SECONDS)
            audio = np.concatenate([overlap_audio, new_audio]) if len(overlap_audio) > 0 else new_audio

            # Save overlap for next chunk
            overlap_samples = int(OVERLAP_SECONDS * _actual_sample_rate)
            overlap_audio = audio[-overlap_samples:] if len(audio) > overlap_samples else audio

            # Skip if audio is mostly silence
            rms = np.sqrt(np.mean(audio.astype(float) ** 2))
            if rms < _silence_threshold():
                continue

            # Transcribe
            path = _save_wav(audio)
            text = _transcribe(path)
            if not text:
                continue

            print(f"[SmartListen] Heard: {text}")
            _transcript_history.append(text)
            _transcript_history = _transcript_history[-CONTEXT_LIMIT:]

            # Notify UI of transcript
            if _on_transcript:
                _on_transcript(text)

            # Generate suggestion in parallel
            def _suggest(t=text):
                suggestion = _generate_suggestion(t)
                if suggestion and _on_suggestion:
                    _on_suggestion(suggestion)

            threading.Thread(target=_suggest, daemon=True).start()

        except Exception as e:
            if _running:
                print(f"[SmartListen] Error: {e}")
                time.sleep(1)


def auto_configure_blackhole() -> str:
    """
    Automatically set up the Multi-Output Device for call audio capture.
    Creates Multi-Output Device with BlackHole + Built-in Output via AppleScript.
    """
    import subprocess
    script = """
    tell application "Audio MIDI Setup"
        activate
    end tell
    delay 1
    tell application "System Events"
        tell process "Audio MIDI Setup"
            -- Click the + button to add device
            click button 1 of group 1 of window 1
            delay 0.5
            -- Click "Create Multi-Output Device"
            click menu item "Create Multi-Output Device" of menu 1
            delay 1
        end tell
    end tell
    """
    try:
        subprocess.run(["osascript", "-e", script], timeout=10)
        return ("Opened Audio MIDI Setup. Please:\n"
                "1. Check both 'BlackHole 2ch' and 'Built-in Output'\n"
                "2. Close the window\n"
                "3. Set 'Multi-Output Device' as your system output in Sound Settings\n"
                "4. In your meeting app, set speakers to Multi-Output Device\n"
                "Then press Cmd+Shift+M to start Smart Listen.")
    except Exception as e:
        return (f"Could not auto-configure ({e}). "
                "Please manually create a Multi-Output Device in Audio MIDI Setup "
                "with both BlackHole 2ch and Built-in Output checked.")


def start(on_transcript=None, on_suggestion=None) -> str:
    """Start smart listening. Returns status message."""
    global _running, _thread, _on_transcript, _on_suggestion, _transcript_history

    if _running:
        return "Smart listening is already active."

    _on_transcript = on_transcript
    _on_suggestion = on_suggestion
    _transcript_history = []

    preferred = preferred_source_snapshot()
    set_device(preferred["device_index"])
    source = _device_name(_device_index)
    guidance = (
        "" if not preferred.get("fallback")
        else " For cleaner direct call audio later, BlackHole 2ch is optional."
    )

    _running = True
    _thread = threading.Thread(target=_listen_loop, daemon=True, name="SmartListen")
    _thread.start()

    rate = _resolve_device_sample_rate(_device_index)
    return (
        f"Smart listening is active via {source} at {rate}Hz."
        f"{guidance} Suggestions will appear as the conversation unfolds."
    )


def stop() -> str:
    """Stop smart listening."""
    global _running
    _running = False
    return "Smart listening stopped."


def is_running() -> bool:
    return _running


def get_transcript() -> str:
    """Return full transcript so far."""
    return "\n".join(_transcript_history)
