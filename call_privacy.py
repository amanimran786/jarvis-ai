import os
import threading
import time

_enabled = os.getenv("JARVIS_MEETING_SAFE_MODE", "1").strip().lower() not in {"0", "false", "off", "no"}
_lock = threading.Lock()
_cache_until = 0.0
_cached_meeting = "NONE"


def is_enabled() -> bool:
    with _lock:
        return _enabled


def set_enabled(value: bool) -> bool:
    global _enabled
    with _lock:
        _enabled = bool(value)
    return _enabled


def toggle_enabled() -> bool:
    global _enabled
    with _lock:
        _enabled = not _enabled
        return _enabled


def _meeting_label() -> str:
    global _cache_until, _cached_meeting
    now = time.monotonic()
    if now < _cache_until:
        return _cached_meeting
    try:
        import overlay
        meeting = overlay.detect_meeting_app() or "NONE"
    except Exception:
        meeting = "NONE"
    _cached_meeting = meeting
    _cache_until = now + 1.2
    return meeting


def should_suppress_audio() -> bool:
    return is_enabled() and _meeting_label() != "NONE"


def snapshot() -> dict:
    meeting = _meeting_label()
    enabled = is_enabled()
    return {
        "enabled": enabled,
        "meeting": meeting,
        "suppressing_audio": enabled and meeting != "NONE",
    }


def status_text() -> str:
    state = snapshot()
    if state["suppressing_audio"]:
        return f"Meeting-safe mode is ON. Audio replies are suppressed during {state['meeting']} calls."
    if state["enabled"]:
        return "Meeting-safe mode is ON. Jarvis will stay quiet automatically when a call is detected."
    return "Meeting-safe mode is OFF. Jarvis may speak aloud during calls."
