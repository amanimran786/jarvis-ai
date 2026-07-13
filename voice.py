import logging
import os
import tempfile
import subprocess
import threading
import time as _time
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
from local_runtime.audio_capture_guard import audio_capture
# Prefer the subprocess bridge when it imports cleanly, but keep runtime stable
# while that path is still being debugged.
try:
    from local_runtime import local_kokoro_subprocess_tts as local_kokoro_tts
except Exception:
    from local_runtime import local_kokoro_tts

_openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
_recognizer = sr.Recognizer()

# Real-time microphone audio amplitude/decibel tracker (normalized between 0.0 and 1.0)
_mic_level = 0.0

def get_mic_level() -> float:
    """Return the current thread-safe microphone amplitude level (0.0 to 1.0)."""
    global _mic_level
    return _mic_level

WAKE_WORDS = {"jarvis", "hey jarvis", "ok jarvis", "okay jarvis"}
_last_tts_engine = ""
MANUAL_PROMPT_WINDOW_SECONDS = 8.0
# Max seconds of speech captured once the user starts talking (endpointed
# listen). The window ends automatically on a natural pause well before this.
MANUAL_PROMPT_PHRASE_LIMIT = 15.0
WAKE_WORD_WINDOW_SECONDS = 3.0
_kokoro_disabled_reason = ""

# Preferred real microphones — keep meeting/loopback devices out of normal voice.
_PREFERRED_MICS = ["MacBook Pro Microphone", "AirPods Pro", "iPhone Microphone", "Built-in Microphone"]
_VOICE_DEVICE_SKIP = [
    "blackhole",
    "loopback",
    "virtual",
    "microsoft teams audio",
    "zoom audio",
    "zoomaudio",
    "aggregate",
    "aggregate device",
    "multi-output",
    "soundflower",
    "background music",
    "eqmac",
    "obs virtual",
]
_VOICE_LOG_PATH = Path.home() / "Library" / "Application Support" / "Jarvis" / ".jarvis_voice.log"
_MIC_OPEN_RETRY_SECONDS = 5.0
_MIC_OPEN_TIMEOUT_SECONDS = 5.0
_MIC_DEVICE_RETRY_SECONDS = 60.0
_MIC_SKIP_LOGGED: set[str] = set()
_MIC_RECENT_FAILURES: dict[str, float] = {}
_MIC_OPEN_LOCK = threading.Lock()
# PyAudio microphone lifecycle calls can crash when run concurrently.
_PYAUDIO_MIC_LOCK = threading.Lock()
_mic_failure_cooldown_until = 0.0
_mic_last_failure_detail = ""
_active_mic_label = ""
_mic_open_worker: threading.Thread | None = None


@contextmanager
def _suppress_native_audio_stderr():
    """Suppress PortAudio/CoreAudio stdio noise during device probing.

    PyAudio can print AUHAL errors directly to fd 1/2 before raising or returning
    an unusable stream. Jarvis logs the actionable failure through _debug_log, so
    keep native stderr quiet unless low-level audio debugging is explicitly on.
    """
    if os.getenv("JARVIS_AUDIO_DEBUG", "").lower() in {"1", "true", "yes"}:
        yield
        return

    saved_fds = []
    devnull_fd = None
    try:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        for fd in (1, 2):
            saved_fds.append((fd, os.dup(fd)))
            os.dup2(devnull_fd, fd)
    except OSError:
        pass
    try:
        yield
    finally:
        for fd, saved_fd in reversed(saved_fds):
            try:
                os.dup2(saved_fd, fd)
            except OSError:
                pass
            try:
                os.close(saved_fd)
            except OSError:
                pass
        if devnull_fd is not None:
            try:
                os.close(devnull_fd)
            except OSError:
                pass


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
        logging.debug("[Voice] silent failure in _debug_log", exc_info=True)


def _get_microphone() -> sr.Microphone:
    """Return a Microphone on a real input device, skipping virtual loopback buses."""
    candidates = _microphone_candidates()
    if candidates:
        return candidates[0][1]
    if not _PYAUDIO_MIC_LOCK.acquire(blocking=False):
        raise RuntimeError("Microphone device probe is busy")
    try:
        return sr.Microphone()
    finally:
        _PYAUDIO_MIC_LOCK.release()


def _voice_device_skip_reason(name: str) -> str:
    normalized = (name or "").lower()
    if any(skip in normalized for skip in _VOICE_DEVICE_SKIP):
        return "virtual, aggregate, or meeting audio device"
    return ""


def _mic_candidate_key(label: str) -> str:
    return " ".join((label or "").lower().split())


def _recent_mic_failure_active(label: str) -> bool:
    key = _mic_candidate_key(label)
    until = _MIC_RECENT_FAILURES.get(key, 0.0)
    if until <= _time.monotonic():
        _MIC_RECENT_FAILURES.pop(key, None)
        return False
    return True


def _mark_mic_candidate_failed(label: str) -> None:
    _MIC_RECENT_FAILURES[_mic_candidate_key(label)] = _time.monotonic() + _MIC_DEVICE_RETRY_SECONDS


def _clear_mic_candidate_failure(label: str) -> None:
    _MIC_RECENT_FAILURES.pop(_mic_candidate_key(label), None)


def _input_capable_device_indexes() -> set[int] | None:
    """Return input-capable PyAudio device indexes, or None when unavailable.

    Excludes devices with defaultSampleRate == 0 — these are broken/virtual
    devices that cause PaMacCore AUHAL errors when opened.
    """
    audio = None
    try:
        audio = sr.Microphone.get_pyaudio().PyAudio()
        count = audio.get_device_count()
        indexes: set[int] = set()
        for index in range(count):
            info = audio.get_device_info_by_index(index) or {}
            if int(info.get("maxInputChannels") or 0) <= 0:
                continue
            # Skip devices whose CoreAudio sample rate is unset — these are
            # virtual/aggregate buses that generate PaMacCore AUHAL errors.
            try:
                if float(info.get("defaultSampleRate") or 0) <= 0:
                    continue
            except (TypeError, ValueError):
                pass
            indexes.add(index)
        return indexes
    except Exception:
        return None
    finally:
        try:
            if audio is not None:
                audio.terminate()
        except Exception:
            logging.debug("[Voice] PyAudio terminate failed in device scan", exc_info=True)


def _default_input_device_info() -> dict | None:
    """Return the current PyAudio default input device info when available."""
    audio = None
    try:
        audio = sr.Microphone.get_pyaudio().PyAudio()
        info = audio.get_default_input_device_info() or {}
        if not info:
            return None
        return info
    except Exception:
        return None
    finally:
        try:
            if audio is not None:
                audio.terminate()
        except Exception:
            logging.debug("[Voice] PyAudio terminate failed in default device probe", exc_info=True)


def _microphone_candidates() -> list[tuple[str, sr.Microphone]]:
    """Return microphone candidates ordered by preference.

    Priority:
      1. Exact or substring match against _PREFERRED_MICS (highest priority first).
      2. Fuzzy word match: any single significant word from a preferred name appears
         in the device name (handles e.g. "AirPods" matching "AirPods Pro Mic").
      3. Any real non-virtual, non-meeting device not in _VOICE_DEVICE_SKIP.
      4. macOS default input device as last resort.

    This ensures the Default input device is only used when nothing better exists,
    rather than always winning by being inserted first.

    Raises RuntimeError instead of waiting when another lifecycle call is active.
    """
    if _mic_open_worker is not None and _mic_open_worker.is_alive():
        raise RuntimeError("Microphone device probe unavailable while an open is still running")
    if not _PYAUDIO_MIC_LOCK.acquire(blocking=False):
        raise RuntimeError("Microphone device probe is busy")
    try:
        return _microphone_candidates_locked()
    finally:
        _PYAUDIO_MIC_LOCK.release()


def _microphone_candidates_locked() -> list[tuple[str, sr.Microphone]]:
    """Build microphone candidates while the caller owns _PYAUDIO_MIC_LOCK."""
    candidates: list[tuple[str, sr.Microphone]] = []
    seen: set[int | None] = set()

    try:
        names = sr.Microphone.list_microphone_names() or []
    except Exception:
        names = []
    input_indexes = _input_capable_device_indexes()
    default_info = _default_input_device_info()

    def _is_candidate_input(index: int | None, label: str) -> bool:
        skip_reason = _voice_device_skip_reason(label)
        if skip_reason:
            skip_key = f"{index}:{label}:skip"
            if skip_key not in _MIC_SKIP_LOGGED:
                _MIC_SKIP_LOGGED.add(skip_key)
                _debug_log(f"[Mic] Skipping {label}: {skip_reason}")
            return False
        if _recent_mic_failure_active(label):
            return False
        if index is None:
            if default_info:
                default_name = str(default_info.get("name") or "Default input device")
                default_index = default_info.get("index")
                default_channels = int(default_info.get("maxInputChannels") or 0)
                default_skip_reason = _voice_device_skip_reason(default_name)
                if default_skip_reason:
                    skip_key = f"default:{default_name}:skip"
                    if skip_key not in _MIC_SKIP_LOGGED:
                        _MIC_SKIP_LOGGED.add(skip_key)
                        _debug_log(f"[Mic] Skipping default input device ({default_name}): {default_skip_reason}")
                    return False
                if default_channels <= 0:
                    skip_key = f"default:{default_name}:channels"
                    if skip_key not in _MIC_SKIP_LOGGED:
                        _MIC_SKIP_LOGGED.add(skip_key)
                        _debug_log(f"[Mic] Skipping default input device ({default_name}): no input channels")
                    return False
                try:
                    if int(default_index) in seen:
                        return False
                except (TypeError, ValueError):
                    pass
            return True
        if input_indexes is None:
            return True
        if index in input_indexes:
            return True
        skip_key = f"{index}:{label}"
        if skip_key not in _MIC_SKIP_LOGGED:
            _MIC_SKIP_LOGGED.add(skip_key)
            _debug_log(f"[Mic] Skipping {label}: no input channels")
        return False

    def _append(index: int | None, label: str) -> None:
        if index in seen:
            return
        if not _is_candidate_input(index, label):
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
        if not any(skip in name.lower() for skip in _VOICE_DEVICE_SKIP):
            _append(i, name)

    # Tier 4: macOS default input, but only if PyAudio says it is a real input.
    _append(None, "Default input device")

    return candidates


def _close_cancelled_microphone_source(microphone, source) -> None:
    with _PYAUDIO_MIC_LOCK:
        try:
            microphone.__exit__(None, None, None)
            return
        except Exception:
            logging.debug("[Voice] timed-out microphone __exit__ failed", exc_info=True)

        try:
            stream = getattr(source, "stream", None)
            if stream is not None:
                stream.close()
            audio = getattr(source, "audio", None)
            if audio is not None:
                audio.terminate()
        except Exception:
            logging.debug("[Voice] timed-out mic cleanup failed", exc_info=True)


def _enter_microphone_with_timeout(label: str, microphone):
    """Bound a native CoreAudio open without starting overlapping AUHAL calls."""
    global _mic_open_worker

    if _mic_open_worker is not None and _mic_open_worker.is_alive():
        raise RuntimeError("Previous microphone open is still blocked in CoreAudio")

    completed = threading.Event()
    state_lock = threading.Lock()
    state = {"cancelled": False, "source": None, "error": None}

    def _open_target() -> None:
        source = None
        try:
            with _PYAUDIO_MIC_LOCK:
                source = microphone.__enter__()
            with state_lock:
                if not state["cancelled"]:
                    state["source"] = source
                    source = None
        except Exception as exc:
            with state_lock:
                state["error"] = exc
        finally:
            if source is not None:
                _close_cancelled_microphone_source(microphone, source)
            completed.set()

    worker = threading.Thread(target=_open_target, name="jarvis-mic-open", daemon=True)
    _mic_open_worker = worker
    with _suppress_native_audio_stderr():
        worker.start()
        finished = completed.wait(timeout=_MIC_OPEN_TIMEOUT_SECONDS)

    if not finished:
        with state_lock:
            state["cancelled"] = True
            late_source = state["source"]
            state["source"] = None
        if late_source is not None:
            _close_cancelled_microphone_source(microphone, late_source)
        raise TimeoutError(
            f"{label} microphone open timed out after {_MIC_OPEN_TIMEOUT_SECONDS:.1f}s"
        )

    _mic_open_worker = None
    if state["error"] is not None:
        raise state["error"]
    return state["source"]


@contextmanager
def _open_microphone_source():
    """Open a live microphone stream, skipping candidates that fail to provide one."""
    global _mic_failure_cooldown_until, _mic_last_failure_detail, _active_mic_label
    last_error: Exception | None = None
    with audio_capture("voice-microphone"), _MIC_OPEN_LOCK:
        now = _time.monotonic()
        if now < _mic_failure_cooldown_until:
            detail = _mic_last_failure_detail or "microphone retry cooldown active"
            raise RuntimeError(f"Microphone retry cooldown active. {detail}")
        if _mic_open_worker is not None and _mic_open_worker.is_alive():
            raise RuntimeError("Previous microphone open is still blocked in CoreAudio")

        for label, microphone in _microphone_candidates():
            if _recent_mic_failure_active(label):
                continue
            source = None
            try:
                source = _enter_microphone_with_timeout(label, microphone)
                if getattr(source, "stream", None) is None:
                    raise RuntimeError(f"{label} opened without a live input stream")
                _debug_log(f"[Mic] Using input device: {label}")
                _active_mic_label = label
                _clear_mic_candidate_failure(label)
                _mic_failure_cooldown_until = 0.0
                _mic_last_failure_detail = ""
                try:
                    # Securely tap source.stream.read to extract real-time RMS audio levels
                    _stream = getattr(source, "stream", None)
                    if _stream is not None and callable(getattr(_stream, "read", None)):
                        original_read = _stream.read
                        def _wrapped_read(*args, **kwargs):
                            chunk = original_read(*args, **kwargs)
                            try:
                                import struct
                                import math
                                count = len(chunk) // 2
                                if count > 0:
                                    shorts = struct.unpack(f"<{count}h", chunk)
                                    sum_squares = sum(x*x for x in shorts)
                                    rms = math.sqrt(sum_squares / count)
                                    global _mic_level
                                    _mic_level = min(1.0, rms / 8000.0)
                            except Exception:
                                logging.debug("[Voice] RMS mic level calc failed", exc_info=True)
                            return chunk
                        source.stream.read = _wrapped_read

                    yield source
                finally:
                    global _mic_level
                    _mic_level = 0.0
                    with _PYAUDIO_MIC_LOCK:
                        microphone.__exit__(None, None, None)
                return
            except Exception as exc:
                last_error = exc
                _mark_mic_candidate_failed(label)
                _debug_log(f"[Mic] Failed to open {label}: {exc}")
                try:
                    if source is not None:
                        with _PYAUDIO_MIC_LOCK:
                            stream = getattr(source, "stream", None)
                            audio = getattr(source, "audio", None)
                            if stream is not None:
                                stream.close()
                            if audio is not None:
                                audio.terminate()
                            source.stream = None
                except Exception:
                    logging.debug("[Voice] mic stream cleanup failed for %s", label, exc_info=True)
                if isinstance(exc, TimeoutError):
                    break

        detail = str(last_error) if last_error is not None else "No microphone devices are available."
        _mic_last_failure_detail = detail
        _mic_failure_cooldown_until = _time.monotonic() + _MIC_OPEN_RETRY_SECONDS
        raise RuntimeError(f"Jarvis could not open a usable microphone input. {detail}")


def _capture_audio_window(source, *, duration: float, reason: str, endpoint: bool = False):
    """
    Record audio from source with a hard timeout to prevent PortAudio/CoreAudio blocking/freezes.

    endpoint=True switches from a fixed-length ``record(duration)`` to
    energy-based ``listen()``, which returns as soon as the speaker stops
    talking (up to ``duration`` to start, ``MANUAL_PROMPT_PHRASE_LIMIT`` of
    speech). This removes the multi-second dead wait on every short command.
    Returns None when no speech was detected within the window.
    """
    if endpoint:
        _debug_log(f"[Mic] Listening (endpointed, ≤{duration:.0f}s to start) for {reason}.")
    else:
        _debug_log(f"[Mic] Recording {duration:.1f}s audio window for {reason}.")

    result = []
    exception_holder = []
    no_speech = []

    def record_target():
        try:
            if endpoint:
                audio_data = _recognizer.listen(
                    source,
                    timeout=duration,
                    phrase_time_limit=MANUAL_PROMPT_PHRASE_LIMIT,
                )
            else:
                audio_data = _recognizer.record(source, duration=duration)
            result.append(audio_data)
        except sr.WaitTimeoutError:
            # No speech began within the window — not an error, just silence.
            no_speech.append(True)
        except Exception as e:
            exception_holder.append(e)

    record_thread = threading.Thread(target=record_target, daemon=True)
    record_thread.start()

    # Freeze watchdog: bound the wait so a wedged PyAudio read can't hang the
    # loop. Endpointed listen() can legitimately run up to start-timeout plus a
    # full phrase, so size the watchdog to its worst case.
    if endpoint:
        timeout = duration + MANUAL_PROMPT_PHRASE_LIMIT + 3.0
    else:
        timeout = duration + 3.0
    record_thread.join(timeout=timeout)

    if record_thread.is_alive():
        _debug_log(f"[Mic] Stream freeze detected for {reason}! PyAudio read blocked for > {timeout:.1f}s.")
        # Try to force terminate PyAudio / close stream to clean up
        try:
            with _PYAUDIO_MIC_LOCK:
                if getattr(source, "stream", None) is not None:
                    source.stream.close()
                if getattr(source, "audio", None) is not None:
                    source.audio.terminate()
        except Exception:
            logging.debug("[Voice] silent failure in record_target", exc_info=True)
        raise RuntimeError(f"Audio stream frozen or dead during record for {reason}")

    if exception_holder:
        raise exception_holder[0]

    if no_speech:
        return None

    if not result:
        raise RuntimeError(f"No audio captured from stream for {reason}")

    return result[0]

# Prevents mic from picking up Jarvis's own TTS output.
# Cleared while Jarvis is speaking; listen() blocks until set again.
_done_speaking = threading.Event()
_done_speaking.set()  # initially not speaking
_stop_requested = threading.Event()
_manual_wake_trigger = threading.Event()  # set by UI to skip wake-word wait

# ── Ambient noise calibration cache ──────────────────────────────────────────
# Calibrate once per session and cache the energy threshold.
# adjust_for_ambient_noise() costs 300ms per call — we call it once and reuse.
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
            
        # Run calibration with a timeout thread to prevent PortAudio freezes
        exception_holder = []
        def calibrate_target():
            try:
                _recognizer.adjust_for_ambient_noise(source, duration=0.3)
            except Exception as e:
                exception_holder.append(e)
                
        cal_thread = threading.Thread(target=calibrate_target, daemon=True)
        cal_thread.start()
        cal_thread.join(timeout=2.0)  # should take 0.3s; 2.0s is extremely safe
        
        if cal_thread.is_alive():
            _debug_log("[Mic] Calibration freeze detected! Audio stream read blocked during calibrate.")
            raise RuntimeError("Audio stream frozen during calibration")
            
        if exception_holder:
            raise exception_holder[0]
            
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


def _local_stt_reported_no_speech(result: dict) -> bool:
    """Return whether local STT authoritatively reported no speech."""
    if (result.get("text") or "").strip():
        return False
    if result.get("ok") is True:
        return True
    return (result.get("error") or "").strip().lower() == "empty transcript"


def _transcribe_wav_bytes(wav_bytes: bytes) -> str | None:
    """Transcribe WAV bytes in memory — no disk I/O."""
    local_result = local_stt.transcribe_audio(wav_bytes, language="en")
    if local_result.get("ok"):
        text = (local_result.get("text") or "").strip()
        if text:
            _debug_log(f"[STT] Transcribed locally via {local_result.get('engine')}.")
            return text
    if _local_stt_reported_no_speech(local_result):
        _debug_log("[STT] Local engine returned no speech; skipping cloud fallback.")
        return None

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
    if _local_stt_reported_no_speech(local_result):
        _debug_log("[STT] Local engine returned no speech; skipping cloud fallback.")
        return None

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
                endpoint=True,
            )
    except Exception as exc:
        _debug_log(f"[Mic] listen failed: {exc}")
        if _active_mic_label:
            _mark_mic_candidate_failed(_active_mic_label)
        return None

    if audio is None:
        _debug_log("[Mic] No speech detected in listen window.")
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
        if _local_stt_reported_no_speech(local_result):
            return None
        local_error = (local_result.get("error") or "").strip()
        if local_error:
            _debug_log(f"[Wake STT] {local_error}")
    finally:
        os.unlink(tmp_path)

    try:
        import model_router

        allow_remote_fallback = not model_router.is_open_source_mode()
    except Exception:
        # Fail closed: if we cannot confirm the mode, assume open-source/local
        # and do not send audio off-device.
        allow_remote_fallback = False

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
                if _stop_requested.is_set():
                    return
                if _manual_wake_trigger.is_set():
                    _manual_wake_trigger.clear()
                    _debug_log("\n[Wake word manually triggered]")
                    return
                audio = _capture_audio_window(
                    source,
                    duration=WAKE_WORD_WINDOW_SECONDS,
                    reason="wake word",
                )
        except Exception as exc:
            _debug_log(f"[Mic] wait_for_wake_word failed: {exc}")
            if _active_mic_label:
                _mark_mic_candidate_failed(_active_mic_label)
            _time.sleep(_MIC_OPEN_RETRY_SECONDS)
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
