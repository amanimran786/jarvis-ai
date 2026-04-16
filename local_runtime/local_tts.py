from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Any

from config import LOCAL_TTS_ENABLED, LOCAL_TTS_RATE_WPM, LOCAL_TTS_VOICE


DEFAULT_ENGINE = "say"
DEFAULT_MACOS_VOICE = "Samantha"
DEFAULT_RATE_WPM = 175
MAX_TEXT_LENGTH = 4000
_VOICE_CACHE: list[str] | None = None
_PREFERRED_VOICES = [
    "Reed (English (US))",
    "Eddy (English (US))",
    "Flo (English (US))",
    "Samantha",
]


def _available_voices() -> list[str]:
    global _VOICE_CACHE
    if _VOICE_CACHE is not None:
        return _VOICE_CACHE
    try:
        proc = subprocess.run(
            [_say_binary(), "-v", "?"],
            capture_output=True,
            text=True,
            check=False,
        )
        voices: list[str] = []
        for line in (proc.stdout or "").splitlines():
            name = line[:20].strip()
            if name:
                voices.append(name)
        _VOICE_CACHE = voices
        return voices
    except Exception:
        _VOICE_CACHE = []
        return _VOICE_CACHE

def _configured_voice() -> str:
    requested = (LOCAL_TTS_VOICE or "").strip()
    available = _available_voices()
    if requested and requested in available:
        return requested
    for preferred in _PREFERRED_VOICES:
        if preferred in available:
            return preferred
    return requested or DEFAULT_MACOS_VOICE


def _configured_rate() -> int:
    rate = LOCAL_TTS_RATE_WPM or DEFAULT_RATE_WPM
    return max(80, min(rate, 420))


def _configured_enabled() -> bool:
    return LOCAL_TTS_ENABLED


def _say_binary() -> str:
    return shutil.which("say") or "/usr/bin/say"


def available() -> bool:
    return sys.platform == "darwin" and os.path.exists(_say_binary())


def config() -> dict[str, Any]:
    return {
        "engine": DEFAULT_ENGINE,
        "enabled": _configured_enabled(),
        "voice": _configured_voice(),
        "rate_wpm": _configured_rate(),
        "platform": sys.platform,
        "binary": _say_binary(),
        "available": available(),
        "max_text_length": MAX_TEXT_LENGTH,
    }


def status() -> dict[str, Any]:
    cfg = config()
    ready = bool(cfg["enabled"] and cfg["available"])
    return {
        **cfg,
        "ready": ready,
        "reason": "" if ready else _unavailable_reason(cfg),
    }


def _unavailable_reason(cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or config()
    if not cfg["enabled"]:
        return "local TTS disabled by configuration"
    if cfg["platform"] != "darwin":
        return f"unsupported platform: {cfg['platform']}"
    if not cfg["available"]:
        return f"'say' command not found at {cfg['binary']}"
    return ""


def _normalize_text(text: str) -> str:
    normalized = " ".join((text or "").split()).strip()
    if len(normalized) > MAX_TEXT_LENGTH:
        normalized = normalized[:MAX_TEXT_LENGTH].rstrip()
    return normalized


def speak(text: str) -> dict[str, Any]:
    cfg = config()
    normalized = _normalize_text(text)

    if not normalized:
        return {
            "ok": False,
            "engine": DEFAULT_ENGINE,
            "spoken": False,
            "text": "",
            "voice": cfg["voice"],
            "rate_wpm": cfg["rate_wpm"],
            "error": "empty text",
            "returncode": None,
        }

    if not cfg["enabled"] or not cfg["available"]:
        return {
            "ok": False,
            "engine": DEFAULT_ENGINE,
            "spoken": False,
            "text": normalized,
            "voice": cfg["voice"],
            "rate_wpm": cfg["rate_wpm"],
            "error": _unavailable_reason(cfg),
            "returncode": None,
        }

    cmd = [
        cfg["binary"],
        "-v",
        cfg["voice"],
        "-r",
        str(cfg["rate_wpm"]),
        normalized,
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        return {
            "ok": False,
            "engine": DEFAULT_ENGINE,
            "spoken": False,
            "text": normalized,
            "voice": cfg["voice"],
            "rate_wpm": cfg["rate_wpm"],
            "error": str(exc),
            "returncode": None,
        }

    stderr = (proc.stderr or "").strip()
    stdout = (proc.stdout or "").strip()
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "engine": DEFAULT_ENGINE,
        "spoken": ok,
        "text": normalized,
        "voice": cfg["voice"],
        "rate_wpm": cfg["rate_wpm"],
        "error": "" if ok else (stderr or stdout or f"say exited with {proc.returncode}"),
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }
