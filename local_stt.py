from __future__ import annotations

import os
import threading
from typing import Any

from config import (
    OPENAI_API_KEY,
    OPENAI_STT_FALLBACK_ENABLED,
    LOCAL_STT_COMPUTE_TYPE,
    LOCAL_STT_DEVICE,
    LOCAL_STT_ENGINE,
    LOCAL_STT_LANGUAGE,
    LOCAL_STT_MODEL,
)


_MODEL_LOCK = threading.Lock()
_MODEL = None
_IMPORT_ERROR: str = ""


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
        "device": LOCAL_STT_DEVICE,
        "compute_type": LOCAL_STT_COMPUTE_TYPE,
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
            device=LOCAL_STT_DEVICE,
            compute_type=LOCAL_STT_COMPUTE_TYPE,
        )
        return _MODEL


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
        model = _get_model()
        segments, info = model.transcribe(
            path,
            language=language or LOCAL_STT_LANGUAGE or None,
            beam_size=_env_int("LOCAL_STT_BEAM_SIZE", 1),
            vad_filter=_env_bool("LOCAL_STT_VAD_FILTER", True),
            condition_on_previous_text=False,
        )
        text = " ".join(segment.text.strip() for segment in segments if segment.text).strip()
        return {
            "ok": bool(text),
            "engine": "faster-whisper",
            "text": text,
            "language": getattr(info, "language", LOCAL_STT_LANGUAGE),
            "error": "" if text else "empty transcript",
        }
    except Exception as exc:
        return {
            "ok": False,
            "engine": "faster-whisper",
            "text": "",
            "error": str(exc),
        }
