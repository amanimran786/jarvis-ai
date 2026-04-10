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
import local_stt
from provider_priority import ask_with_priority
import semantic_memory as _smem
import interview_profile as _ip

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

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
_active_source  = None   # current source snapshot dict
_transcript_history = []
_actual_sample_rate = SAMPLE_RATE  # resolved at start()
_source_history = []
_last_error = ""
_last_transcript = ""
_last_transcript_at = 0.0
_last_suggestion = ""
_last_suggestion_at = 0.0
_last_suggestion_source = ""
_last_interpreted_question = ""
_last_interpreted_question_at = 0.0
_last_audio_rms = 0.0
_last_audio_at = 0.0
_last_silence_at = 0.0
_silence_streak = 0
_empty_transcript_streak = 0
_caption_fallback_active = False
_last_caption = ""
_last_caption_at = 0.0
_last_caption_probe_at = 0.0
_last_meeting_label = None
_source_rotation_count = 0
_listen_started_at = 0.0
_suggestion_model_failures = 0
_suggestion_fallbacks = 0
_last_stt_backend = ""
_last_stt_backend_detail = ""
_event_log: list[str] = []

_SILENCE_FAILOVER_CHUNKS = 3
_EMPTY_TRANSCRIPT_FAILOVER_CHUNKS = 2
_CAPTION_FALLBACK_AFTER_SILENCE = 2
_CAPTION_PROBE_COOLDOWN = 4.0
_EVENT_LOG_LIMIT = 30
_DEVICE_CACHE_TTL = 1.5
_SOURCE_CACHE_TTL = 1.0

_CACHE_LOCK = threading.Lock()
_AUDIO_DEVICES_CACHE: list[dict] | None = None
_AUDIO_DEVICES_CACHE_UNTIL = 0.0
_PREFERRED_SOURCE_CACHE: dict | None = None
_PREFERRED_SOURCE_CACHE_UNTIL = 0.0


def list_audio_devices(force_refresh: bool = False) -> list[dict]:
    """Return all available audio input devices."""
    global _AUDIO_DEVICES_CACHE, _AUDIO_DEVICES_CACHE_UNTIL
    now = time.monotonic()
    with _CACHE_LOCK:
        if not force_refresh and _AUDIO_DEVICES_CACHE is not None and now < _AUDIO_DEVICES_CACHE_UNTIL:
            return [dict(item) for item in _AUDIO_DEVICES_CACHE]

    devices = []
    for i, d in enumerate(sd.query_devices()):
        if d['max_input_channels'] > 0:
            devices.append({"index": i, "name": d['name'], "channels": d['max_input_channels']})
    with _CACHE_LOCK:
        _AUDIO_DEVICES_CACHE = [dict(item) for item in devices]
        _AUDIO_DEVICES_CACHE_UNTIL = time.monotonic() + _DEVICE_CACHE_TTL
    return devices


def get_blackhole_device() -> int | None:
    """Auto-detect BlackHole device index."""
    for d in list_audio_devices():
        if "blackhole" in d['name'].lower():
            return d['index']
    return None


def get_virtual_meeting_audio_device(meeting_label: str | None = None, force_refresh: bool = False) -> int | None:
    """Prefer native meeting audio loopback devices when available."""
    if meeting_label is None:
        try:
            import overlay
            meeting_label = overlay.detect_meeting_app(force_refresh=force_refresh)
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

    for d in list_audio_devices(force_refresh=force_refresh):
        name = d["name"].lower()
        if any(marker in name for marker in preferred_markers):
            return d["index"]
    if preferred_markers != generic_markers:
        for d in list_audio_devices(force_refresh=force_refresh):
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
        for item in list_audio_devices():
            if item.get("index") == idx:
                return str(item.get("name") or "default input")
        info = sd.query_devices(idx)
        return str(info["name"])
    except Exception:
        return "default input"


def _log_event(message: str) -> None:
    ts = time.strftime("%H:%M:%S")
    entry = f"[{ts}] {message}"
    print(f"[SmartListen] {message}")
    _event_log.append(entry)
    del _event_log[:-_EVENT_LOG_LIMIT]


def _meeting_label(force_refresh: bool = False) -> str | None:
    try:
        import overlay
        return overlay.detect_meeting_app(force_refresh=force_refresh)
    except Exception:
        return None


def _source_snapshot(kind: str, device_index: int | None, device_name: str, fallback: bool, reason: str = "") -> dict:
    return {
        "kind": kind,
        "device_index": device_index,
        "device_name": device_name,
        "fallback": fallback,
        "reason": reason,
    }


def _build_source_candidates(force_refresh: bool = False) -> list[dict]:
    """
    Build an ordered list of audio sources to try.
    Meeting-specific sources outrank BlackHole, and BlackHole outranks mic only
    if nothing better is available. This keeps us from getting stuck on an
    installed but unrouted loopback device.
    """
    meeting_label = _meeting_label(force_refresh=force_refresh)
    candidates: list[dict] = []

    meeting_audio = get_virtual_meeting_audio_device(meeting_label=meeting_label, force_refresh=force_refresh)
    if meeting_audio is not None:
        candidates.append(_source_snapshot(
            "meeting_audio",
            meeting_audio,
            _device_name(meeting_audio),
            False,
            reason=f"meeting={meeting_label or 'unknown'}",
        ))

    blackhole = get_blackhole_device()
    if blackhole is not None:
        candidates.append(_source_snapshot(
            "system_audio",
            blackhole,
            _device_name(blackhole),
            False,
            reason="BlackHole",
        ))

    mic = get_preferred_microphone_device()
    if mic is not None:
        candidates.append(_source_snapshot(
            "microphone",
            mic,
            _device_name(mic),
            True,
            reason="microphone fallback",
        ))

    # De-duplicate by device index while preserving priority order.
    seen: set[int | None] = set()
    ordered: list[dict] = []
    for item in candidates:
        key = item.get("device_index")
        if key in seen:
            continue
        seen.add(key)
        ordered.append(item)

    if not ordered:
        ordered.append(_source_snapshot("microphone", None, "default input", True, reason="fallback unavailable"))

    return ordered


def _select_source_snapshot(force_refresh: bool = False) -> dict:
    candidates = _build_source_candidates(force_refresh=force_refresh)
    if not candidates:
        return _source_snapshot("microphone", None, "default input", True, reason="no candidates")

    # Prefer a candidate that is already active if possible.
    active_index = _device_index
    if active_index is not None:
        for item in candidates:
            if item.get("device_index") == active_index:
                return item

    return candidates[0]


def _activate_source(source: dict, reason: str = "") -> None:
    global _device_index, _active_source, _source_rotation_count, _last_error
    if source is None:
        return

    previous = _active_source or {}
    previous_index = previous.get("device_index")
    new_index = source.get("device_index")
    if previous_index == new_index and previous.get("kind") == source.get("kind"):
        _active_source = source
        return

    _device_index = new_index
    _active_source = source
    _source_rotation_count += 1
    _source_history.append({
        "ts": time.time(),
        "reason": reason or source.get("reason", ""),
        "kind": source.get("kind", ""),
        "device_index": new_index,
        "device_name": source.get("device_name", ""),
    })
    del _source_history[:-20]
    _last_error = ""
    _log_event(
        f"Source -> {source.get('device_name', 'default input')} "
        f"({source.get('kind', 'unknown')})"
        + (f" [{reason}]" if reason else "")
    )


def _current_source_kind() -> str:
    source = _active_source or {}
    return str(source.get("kind") or "").lower()


def _silence_threshold(source: dict | None = None) -> int:
    source = source or _active_source or {}
    kind = str(source.get("kind") or "").lower()
    name = str(source.get("device_name") or _device_name(_device_index)).lower()
    if kind in {"system_audio", "meeting_audio"} or "blackhole" in name or "audio" in name:
        return SYSTEM_AUDIO_RMS_THRESHOLD
    return MIC_RMS_THRESHOLD


def _should_probe_captions() -> bool:
    if _silence_streak < _CAPTION_FALLBACK_AFTER_SILENCE:
        return False
    if time.monotonic() - _last_caption_probe_at < _CAPTION_PROBE_COOLDOWN:
        return False
    return True


def _extract_latest_caption_line(text: str) -> str:
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("Recent ") or line.startswith("Couldn't "):
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        if line:
            lines.append(line)
    return lines[-1] if lines else ""


def _select_caption_candidate(lines: list[str], source_mode: str = "") -> str:
    best_line = ""
    best_score = -1
    total = len(lines or [])
    for index, raw in enumerate(lines or []):
        line = str(raw or "").strip()
        if not line:
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        if not line:
            continue
        words = line.split()
        if len(words) < 3 and not (_looks_like_question(line) or _looks_technical(line)):
            continue

        score = 0
        if _looks_like_question(line):
            score += 5
        if _looks_technical(line):
            score += 4
        if line.endswith(("?", ".", "!")):
            score += 2
        if len(words) >= 5:
            score += 2
        if len(words) >= 8:
            score += 1

        # Prefer more recent lines only after content quality.
        score += int((index + 1) / max(total, 1) * 2)

        if score > best_score:
            best_score = score
            best_line = line
    return best_line


def _try_caption_fallback() -> str:
    global _last_caption_probe_at, _last_caption, _last_caption_at, _caption_fallback_active, _last_error
    global _last_interpreted_question, _last_interpreted_question_at
    _last_caption_probe_at = time.monotonic()
    try:
        import browser
        snapshot = browser.meeting_caption_snapshot()
    except Exception as e:
        _last_error = f"caption fallback failed: {e}"
        _caption_fallback_active = False
        return ""

    if not snapshot.get("ok"):
        _last_error = snapshot.get("error", "caption fallback unavailable")
        _caption_fallback_active = False
        return ""

    lines = snapshot.get("lines") or []
    merged_lines = snapshot.get("merged_lines") or []
    focus_line = snapshot.get("focus_line", "")
    source_mode = snapshot.get("source_mode", "")
    source_lines = merged_lines or lines
    line = (
        _select_caption_candidate(source_lines, source_mode=source_mode)
        or _normalize_caption_fragment(focus_line)
        or (source_lines[-1].strip() if source_lines else "")
    )
    if source_mode == "innerText" and len(line.split()) < 3:
        _last_error = "caption fallback returned browser UI text instead of captions"
        _caption_fallback_active = False
        return ""
    if not line:
        _caption_fallback_active = False
        return ""

    question_context = _update_active_question_buffer(source_lines, focus_line=focus_line)
    if question_context:
        _last_interpreted_question = question_context
        _last_interpreted_question_at = time.time()

    if line == _last_caption and _last_suggestion_at >= _last_caption_at:
        _caption_fallback_active = False
        return ""

    _last_caption = line
    _last_caption_at = time.time()
    _caption_fallback_active = True
    return line


def _maybe_rotate_source(force_refresh: bool = False, reason: str = "") -> bool:
    """
    Switch to the next best candidate after sustained silence or empty transcripts.
    Returns True when a new source was activated.
    """
    global _silence_streak, _empty_transcript_streak, _last_silence_at, _last_meeting_label

    candidates = _build_source_candidates(force_refresh=force_refresh)
    if not candidates:
        return False

    active_index = _device_index
    next_source = next((candidate for candidate in candidates if candidate.get("device_index") != active_index), None)

    if next_source is not None:
        _activate_source(next_source, reason=reason or "failover")
        _silence_streak = 0
        _empty_transcript_streak = 0
        _last_silence_at = 0.0
        _last_meeting_label = _meeting_label()
        return True
    return False


def preferred_source_snapshot(force_refresh: bool = False) -> dict:
    global _PREFERRED_SOURCE_CACHE, _PREFERRED_SOURCE_CACHE_UNTIL
    now = time.monotonic()
    with _CACHE_LOCK:
        if not force_refresh and _PREFERRED_SOURCE_CACHE is not None and now < _PREFERRED_SOURCE_CACHE_UNTIL:
            return dict(_PREFERRED_SOURCE_CACHE)

    source = _select_source_snapshot(force_refresh=force_refresh)
    snapshot = {
        **source,
        "candidates": _build_source_candidates(force_refresh=force_refresh),
    }
    with _CACHE_LOCK:
        _PREFERRED_SOURCE_CACHE = dict(snapshot)
        _PREFERRED_SOURCE_CACHE_UNTIL = time.monotonic() + _SOURCE_CACHE_TTL
    return snapshot


def status_snapshot() -> dict:
    preferred = preferred_source_snapshot()
    active_index = _device_index if _device_index is not None else preferred["device_index"]
    local_stt_status = local_stt.status()
    local_available = bool(local_stt_status.get("available", local_stt_status.get("local_available", False)))
    return {
        "running": _running,
        "started_at": _listen_started_at,
        "preferred": preferred,
        "active_device_index": active_index,
        "active_device_name": _device_name(active_index),
        "active_source": _active_source or preferred,
        "source_history": list(_source_history[-10:]),
        "source_rotation_count": _source_rotation_count,
        "silence_streak": _silence_streak,
        "empty_transcript_streak": _empty_transcript_streak,
        "caption_fallback_active": _caption_fallback_active,
        "last_caption": _last_caption,
        "last_caption_at": _last_caption_at,
        "last_transcript": _last_transcript,
        "last_transcript_at": _last_transcript_at,
        "last_suggestion": _last_suggestion,
        "last_suggestion_at": _last_suggestion_at,
        "last_suggestion_source": _last_suggestion_source,
        "last_interpreted_question": _last_interpreted_question,
        "last_interpreted_question_at": _last_interpreted_question_at,
        "last_audio_rms": _last_audio_rms,
        "last_audio_at": _last_audio_at,
        "last_silence_at": _last_silence_at,
        "last_error": _last_error,
        "sample_rate": _actual_sample_rate,
        "meeting_label": _last_meeting_label,
        "stt_backend": _last_stt_backend,
        "stt_backend_detail": _last_stt_backend_detail,
        "local_stt_available": local_available,
        "local_stt_status": local_stt_status,
        "suggestion_model_failures": _suggestion_model_failures,
        "suggestion_fallbacks": _suggestion_fallbacks,
        "events": list(_event_log[-10:]),
    }


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
    """Transcribe audio file, preferring local STT before API fallback."""
    global _last_stt_backend, _last_stt_backend_detail, _last_error
    try:
        local_status = local_stt.status()
        local_available = bool(local_status.get("local_available", False))
        openai_fallback_allowed = bool(local_status.get("openai_fallback_allowed", True))
        local_result = local_stt.transcribe_file(path, language="en")
        if local_result.get("ok"):
            _last_stt_backend = local_result.get("engine") or "faster-whisper"
            _last_stt_backend_detail = local_result.get("engine") or "faster-whisper"
            text = (local_result.get("text") or "").strip()
            if _is_hallucination(text):
                return ""
            return text

        local_error = (local_result.get("error") or "").strip()
        if local_error and local_available:
            _last_error = local_error
            _log_event(f"Local STT failed, falling back to API: {local_error}")
        elif local_error and not openai_fallback_allowed:
            _last_error = local_error
            _last_stt_backend = local_status.get("active_engine") or "unavailable"
            _last_stt_backend_detail = local_result.get("engine") or _last_stt_backend
            return ""

        if client is None:
            _last_error = "OpenAI STT fallback is not configured."
            _last_stt_backend = "unavailable"
            _last_stt_backend_detail = "openai-whisper-1"
            return ""

        with open(path, 'rb') as f:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="en"
            )
        _last_stt_backend = "openai"
        _last_stt_backend_detail = "whisper-1"
        text = result.text.strip()
        if _is_hallucination(text):
            return ""
        return text
    except Exception as exc:
        _last_error = str(exc)
        return ""
    finally:
        if os.path.exists(path):
            os.unlink(path)


def _generate_suggestion(new_line: str, question_context: str = "") -> str:
    """
    Given recent transcript context and a new line,
    generate a smart response suggestion.
    """
    if not new_line:
        return ""

    question_context = " ".join((question_context or "").split()).strip()
    interpreted = _interpret_recent_question(new_line)
    if interpreted:
        new_line = interpreted

    if question_context and question_context != new_line:
        if _looks_like_question(question_context) or _looks_technical(question_context):
            new_line = question_context
        else:
            combined = f"{question_context} {new_line}".strip()
            if len(combined.split()) >= len(new_line.split()):
                new_line = combined

    word_count = len(new_line.split())
    if word_count < 4 and not (_looks_like_question(new_line) or _looks_technical(new_line)):
        return ""  # too short to be meaningful unless it is clearly a question or technical prompt

    context = "\n".join(_transcript_history[-10:])
    question_like = _looks_like_question(new_line)
    technical = _looks_technical(new_line + "\n" + context)
    tier = "strong" if (question_like or technical or question_context) else "cheap"
    if question_context:
        mode_line = (
            "The latest line may be a fragment, but the active question buffer makes the intended question clear. Reconstruct the question from the buffer and answer it plainly."
        )
    elif question_like:
        mode_line = "The latest line is a direct question. Answer it plainly and immediately."
    else:
        mode_line = "The latest line is not a direct question. Give the best concise response or follow-up."

    prompt = f"""You are a real-time meeting assistant helping Aman respond during a live call.

Recent conversation transcript:
{context}

Latest thing just said:
"{new_line}"

Active question buffer:
{question_context or "(none)"}

Instructions:
- {mode_line}
- If the latest text arrived in fragments, merge it mentally with the active question buffer before answering.
- Use only the transcript, the active question buffer, and the provided career context. Do not claim to have heard or verified details that are not present there.
- Return exactly what Aman should say next, not analysis about the conversation.
- Keep it clear, concise, and spoken naturally.
- Default to 1-2 sentences.
- If the question is technical, give the precise answer first and the key rationale second.
- If the transcript and question buffer already contain a usable question, answer it directly instead of asking for vague clarification.
- If the transcript is truly too incomplete to answer, ask one short clarification question instead of inventing missing details.
- If there is uncertainty, still give the best answer you can from the available words instead of pretending you heard more than you did.
- Do not say "I suggest", "You could say", "Aman should say", or similar framing.
- Do not use bullets, headers, markdown, or filler.
- Do not invent personal experience that was not stated in the transcript."""

    # Inject Aman's KB context so suggestions use his real background, not generic LLM knowledge.
    # Semantic memory gives relevant facts/stories; interview profile gives structured career answers.
    query_for_kb = new_line
    smem_ctx = _smem.context_for_query(query_for_kb, top_k=3, max_chars=900)
    ip_ctx = _ip.answer_for_query(query_for_kb) if (question_like or technical) else ""
    kb_parts = []
    if smem_ctx:
        kb_parts.append(smem_ctx)
    if ip_ctx and len(ip_ctx) > 80:
        kb_parts.append(f"[Career profile context]\n{ip_ctx[:600]}")
    system_extra = "\n\n".join(kb_parts) if kb_parts else ""

    global _suggestion_model_failures, _suggestion_fallbacks
    try:
        suggestion = ask_with_priority(prompt, tier=tier, system_extra=system_extra)
        if suggestion and suggestion.strip():
            return suggestion.strip()
        _suggestion_model_failures += 1
    except Exception:
        _suggestion_model_failures += 1

    fallback = _fallback_suggestion_text(new_line, question_like=question_like, technical=technical)
    if fallback:
        _suggestion_fallbacks += 1
        return fallback
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


def _update_interpreted_question_buffer(lines: list[str], focus_line: str = "") -> str:
    global _last_interpreted_question, _last_interpreted_question_at

    candidates: list[str] = []
    for raw in (lines or [])[-6:]:
        line = _normalize_caption_fragment(raw)
        if not line or _is_ui_caption_fragment(line):
            continue
        if _looks_like_question(line) or _looks_technical(line) or _looks_like_caption_fragment(line):
            candidates.append(line)

    focus = _normalize_caption_fragment(focus_line)
    if focus and focus not in candidates and not _is_ui_caption_fragment(focus):
        candidates.append(focus)

    if not candidates:
        if _last_interpreted_question and (time.time() - _last_interpreted_question_at) > 8.0:
            _last_interpreted_question = ""
            _last_interpreted_question_at = 0.0
        return _last_interpreted_question

    unique: list[str] = []
    for line in candidates:
        if line not in unique:
            unique.append(line)

    buffer = unique[-4:]
    interpreted = " ".join(buffer[-3:]).strip()
    interpreted = " ".join(interpreted.split()).strip()
    if interpreted:
        _last_interpreted_question = interpreted
        _last_interpreted_question_at = time.time()
    return _last_interpreted_question


def _normalize_caption_fragment(text: str) -> str:
    return " ".join((text or "").replace("\u00a0", " ").split()).strip()


def _is_ui_caption_fragment(text: str) -> bool:
    normalized = _normalize_caption_fragment(text)
    if not normalized:
        return True
    low = normalized.lower()
    if low in {"captions", "subtitle", "subtitles", "chat", "participants", "reactions", "share", "leave"}:
        return True
    words = normalized.split()
    if len(words) == 1 and len(normalized) < 4:
        return True
    if 2 <= len(words) <= 3 and all(word[:1].isupper() and word[1:].islower() for word in words) and not any(ch in normalized for ch in ".?!"):
        return True
    return False


def _looks_like_caption_fragment(text: str) -> bool:
    normalized = _normalize_caption_fragment(text)
    if not normalized or _is_ui_caption_fragment(normalized):
        return False
    if _looks_like_question(normalized) or _looks_technical(normalized):
        return False
    words = normalized.split()
    if len(words) <= 2:
        return True
    if not normalized.endswith((".", "?", "!")) and len(words) <= 6:
        return True
    return False


def _merge_caption_fragment_lines(lines: list[str]) -> list[str]:
    merged: list[str] = []
    for raw in lines or []:
        line = _normalize_caption_fragment(raw)
        if not line or _is_ui_caption_fragment(line):
            continue
        if not merged:
            merged.append(line)
            continue
        prev = merged[-1]
        prev_open = not prev.endswith((".", "?", "!")) or prev.endswith((",", ":", ";"))
        curr_fragment = _looks_like_caption_fragment(line)
        prev_fragment = _looks_like_caption_fragment(prev)
        continuation = line[0].islower()
        if prev_open and (curr_fragment or prev_fragment or continuation):
            merged[-1] = _normalize_caption_fragment(f"{prev} {line}")
        else:
            merged.append(line)
    return merged


def _update_active_question_buffer(lines: list[str], focus_line: str = "") -> str:
    global _last_interpreted_question, _last_interpreted_question_at

    merged = _merge_caption_fragment_lines(lines)
    if focus_line:
        focus = _normalize_caption_fragment(focus_line)
        if focus and focus not in merged and not _is_ui_caption_fragment(focus):
            merged.append(focus)

    questionish: list[str] = []
    for line in merged:
        if _looks_like_question(line) or _looks_technical(line) or _looks_like_caption_fragment(line):
            questionish.append(line)

    if not questionish:
        if _last_interpreted_question and (time.time() - _last_interpreted_question_at) > 8.0:
            _last_interpreted_question = ""
            _last_interpreted_question_at = 0.0
        return _last_interpreted_question

    # Keep a short rolling buffer and expose the most recent merged question context.
    buffer = questionish[-4:]
    _last_interpreted_question = " ".join(buffer[-3:]).strip()
    _last_interpreted_question_at = time.time()
    return _last_interpreted_question


def _fallback_suggestion_text(new_line: str, question_like: bool, technical: bool) -> str:
    low = " ".join((new_line or "").strip().lower().split())

    if "tell me about yourself" in low or "your background" in low or "work experience" in low or "your experience" in low:
        return "Give a brief summary of your role, what you have been working on, and one concrete result."

    if "what is a variable" in low or "variable" in low:
        return "A variable is a named value used to store data that can change."
    if "function" in low:
        return "A function is a reusable block of code that takes input, does work, and returns a result."
    if "class" in low:
        return "A class is a blueprint for creating objects with shared state and behavior."
    if "api" in low:
        return "An API is an interface that lets one system talk to another in a defined way."

    if question_like or technical:
        return "Answer directly and briefly, then add one short supporting detail."

    if len(low.split()) >= 2:
        return "Acknowledge the point and add one concrete detail."

    return ""


def _interpret_recent_question(new_line: str) -> str:
    global _last_interpreted_question, _last_interpreted_question_at

    lines = [str(item or "").strip() for item in _transcript_history[-4:]]
    lines = [line for line in lines if line]
    current = (new_line or "").strip()
    if current and (not lines or lines[-1] != current):
        lines.append(current)
    if not lines:
        return ""

    unique_lines: list[str] = []
    for line in lines:
        if line not in unique_lines:
            unique_lines.append(line)

    questionish = [line for line in unique_lines if _looks_like_question(line)]
    technical = [line for line in unique_lines if _looks_technical(line)]

    interpreted = ""
    if questionish:
        technical_questionish = [line for line in questionish if _looks_technical(line)]
        if technical_questionish:
            interpreted = technical_questionish[-1]
        else:
            interpreted = questionish[-1]
    elif technical and len(unique_lines) >= 2:
        lead = next(
            (
                line for line in reversed(unique_lines[:-1])
                if any(token in line.lower() for token in ("tell me", "explain", "walk me through", "what", "how", "why", "can you"))
            ),
            "",
        )
        tail = max(technical, key=lambda item: (len(item.split()), len(item)))
        interpreted = f"{lead} {tail}".strip() if lead and lead not in tail else tail
    elif technical:
        interpreted = max(technical, key=lambda item: (len(item.split()), len(item)))
    else:
        interpreted = unique_lines[-1]

    interpreted = " ".join(interpreted.split()).strip()
    if interpreted:
        _last_interpreted_question = interpreted
        _last_interpreted_question_at = time.time()
    return interpreted


def _store_suggestion(suggestion: str, source: str) -> None:
    global _last_suggestion, _last_suggestion_at, _last_suggestion_source
    _last_suggestion = suggestion
    _last_suggestion_at = time.time()
    _last_suggestion_source = source


def _listen_loop():
    """Main listening loop — runs in background thread."""
    global _running, _transcript_history, _actual_sample_rate
    global _silence_streak, _empty_transcript_streak, _last_error, _last_transcript
    global _last_transcript_at, _last_suggestion, _last_suggestion_at
    global _last_audio_rms, _last_audio_at, _last_silence_at, _caption_fallback_active
    global _last_meeting_label

    # Resolve device's native sample rate once before recording
    _actual_sample_rate = _resolve_device_sample_rate(_device_index)
    _log_event(f"Using sample rate: {_actual_sample_rate}Hz")

    # Import speaking guard from voice module
    try:
        from voice import _done_speaking as _speaking_guard
    except ImportError:
        _speaking_guard = None

    overlap_audio = np.array([], dtype='int16')
    _log_event("Listening to call audio...")

    while _running:
        try:
            current_meeting = _meeting_label()
            if current_meeting != _last_meeting_label:
                _last_meeting_label = current_meeting
                _log_event(f"Meeting context -> {current_meeting or 'none'}")

            # Don't record while Jarvis is speaking — avoids transcribing own TTS
            if _speaking_guard and not _speaking_guard.is_set():
                time.sleep(0.1)
                continue

            if _device_index is None:
                preferred = _select_source_snapshot(force_refresh=bool(_silence_streak >= _SILENCE_FAILOVER_CHUNKS))
                _activate_source(preferred, reason="startup" if not _active_source else "refresh")

            # Record chunk (with overlap from last chunk prepended)
            new_audio = _record_chunk(CHUNK_SECONDS - OVERLAP_SECONDS)
            audio = np.concatenate([overlap_audio, new_audio]) if len(overlap_audio) > 0 else new_audio

            # Save overlap for next chunk
            overlap_samples = int(OVERLAP_SECONDS * _actual_sample_rate)
            overlap_audio = audio[-overlap_samples:] if len(audio) > overlap_samples else audio

            # Skip if audio is mostly silence
            rms = np.sqrt(np.mean(audio.astype(float) ** 2))
            _last_audio_rms = float(rms)
            _last_audio_at = time.time()
            threshold = _silence_threshold()
            if rms < threshold:
                _silence_streak += 1
                _last_silence_at = time.time()
                _log_event(
                    f"Silence streak={_silence_streak} rms={rms:.1f} threshold={threshold} "
                    f"source={_device_name(_device_index)}"
                )
                if _silence_streak >= _SILENCE_FAILOVER_CHUNKS:
                    if _should_probe_captions():
                        caption_line = _try_caption_fallback()
                        if caption_line:
                            _last_transcript = caption_line
                            _last_transcript_at = time.time()
                            _transcript_history.append(caption_line)
                            _transcript_history = _transcript_history[-CONTEXT_LIMIT:]
                            _log_event(f"Caption fallback heard: {caption_line}")
                            if _on_transcript:
                                _on_transcript(caption_line)
                            caption_context = _last_interpreted_question
                            def _suggest_caption(t=caption_line):
                                suggestion = _generate_suggestion(t, question_context=caption_context)
                                if suggestion and _on_suggestion:
                                    _on_suggestion(suggestion)
                                if suggestion:
                                    _store_suggestion(suggestion, source="caption")
                            threading.Thread(target=_suggest_caption, daemon=True).start()
                            _silence_streak = 0
                            _empty_transcript_streak = 0
                            continue
                    if _maybe_rotate_source(force_refresh=True, reason="sustained silence"):
                        _caption_fallback_active = False
                        overlap_audio = np.array([], dtype='int16')
                        continue
                continue
            else:
                _silence_streak = 0
                _caption_fallback_active = False

            # Transcribe
            path = _save_wav(audio)
            text = _transcribe(path)
            if not text:
                _empty_transcript_streak += 1
                _last_error = "transcription returned empty transcript"
                if _empty_transcript_streak >= _EMPTY_TRANSCRIPT_FAILOVER_CHUNKS:
                    if _maybe_rotate_source(force_refresh=True, reason="empty transcripts"):
                        overlap_audio = np.array([], dtype='int16')
                        continue
                continue

            _empty_transcript_streak = 0
            _last_transcript = text
            _last_transcript_at = time.time()
            _log_event(f"Heard: {text}")
            _transcript_history.append(text)
            _transcript_history = _transcript_history[-CONTEXT_LIMIT:]

            # Notify UI of transcript
            if _on_transcript:
                _on_transcript(text)

            # Generate suggestion in parallel
            transcript_context = _update_active_question_buffer(_transcript_history[-6:], focus_line=text)
            def _suggest(t=text):
                suggestion = _generate_suggestion(t, question_context=transcript_context)
                if suggestion and _on_suggestion:
                    _on_suggestion(suggestion)
                if suggestion:
                    _store_suggestion(suggestion, source="transcript")

            threading.Thread(target=_suggest, daemon=True).start()

        except Exception as e:
            if _running:
                _last_error = str(e)
                _log_event(f"Error: {e}")
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
    global _active_source, _source_history, _last_error, _last_transcript
    global _last_transcript_at, _last_suggestion, _last_suggestion_at
    global _last_audio_rms, _last_audio_at, _last_silence_at, _silence_streak
    global _empty_transcript_streak, _caption_fallback_active, _last_caption
    global _last_caption_at, _last_caption_probe_at, _last_meeting_label
    global _source_rotation_count, _listen_started_at, _device_index
    global _last_suggestion_source, _suggestion_model_failures, _suggestion_fallbacks
    global _last_interpreted_question, _last_interpreted_question_at
    global _last_stt_backend, _last_stt_backend_detail

    if _running:
        return "Smart listening is already active."

    _on_transcript = on_transcript
    _on_suggestion = on_suggestion
    _transcript_history = []
    _active_source = None
    _source_history = []
    _last_error = ""
    _last_transcript = ""
    _last_transcript_at = 0.0
    _last_suggestion = ""
    _last_suggestion_at = 0.0
    _last_suggestion_source = ""
    _last_interpreted_question = ""
    _last_interpreted_question_at = 0.0
    _last_audio_rms = 0.0
    _last_audio_at = 0.0
    _last_silence_at = 0.0
    _silence_streak = 0
    _empty_transcript_streak = 0
    _caption_fallback_active = False
    _last_caption = ""
    _last_caption_at = 0.0
    _last_caption_probe_at = 0.0
    _last_meeting_label = _meeting_label()
    _source_rotation_count = 0
    _listen_started_at = time.time()
    _suggestion_model_failures = 0
    _suggestion_fallbacks = 0
    _last_stt_backend = ""
    _last_stt_backend_detail = ""

    preferred = _select_source_snapshot(force_refresh=True)
    _activate_source(preferred, reason="start")
    source = _device_name(_device_index)
    guidance = (
        "" if not preferred.get("fallback")
        else " For cleaner direct call audio later, BlackHole 2ch is optional."
    )

    _running = True
    _thread = threading.Thread(target=_listen_loop, daemon=True, name="SmartListen")
    _thread.start()

    rate = _resolve_device_sample_rate(_device_index)
    candidate_text = ", ".join(
        f"{item.get('device_name')} ({item.get('kind')})"
        for item in preferred.get("candidates", [])
    )
    return (
        f"Smart listening is active via {source} at {rate}Hz."
        f"{guidance} Suggestions will appear as the conversation unfolds."
        + (f" Source order: {candidate_text}." if candidate_text else "")
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
