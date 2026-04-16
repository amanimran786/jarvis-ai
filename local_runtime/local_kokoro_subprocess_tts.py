"""
Kokoro TTS via subprocess — bypasses PyInstaller bundling issues entirely.

The frozen Jarvis.app cannot import kokoro-onnx directly (PyInstaller misses
deps). This module calls tts_subprocess.py via the project venv Python instead,
which has all dependencies. The API is identical to local_kokoro_tts.py so
voice.py needs zero changes.

Cold-start elimination:
  A single persistent subprocess (daemon mode) is started at prewarm time.
  The ONNX model is loaded once inside that process and stays in memory.
  Every subsequent synthesis request is a stdin/stdout JSON round-trip —
  no per-sentence process spawning, no model reload.
  If the daemon dies it is restarted transparently (one extra cold-start).
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from config import JARVIS_KOKORO_VOICE, JARVIS_KOKORO_TTS_ENABLED

DEFAULT_ENGINE = "kokoro"
MAX_TEXT_LENGTH = 4000
SUBPROCESS_TIMEOUT = 90      # one-shot fallback: covers first-run model load + synthesis
DAEMON_REQUEST_TIMEOUT = 30  # max seconds to wait for a daemon synthesis response
DAEMON_STARTUP_TIMEOUT = 45  # max seconds to wait for {"ready": true} on launch

# Resolved once at startup; None means subprocess path not found yet
_venv_python: str | None = None
_script_path: str | None = None
_paths_checked = False
_paths_lock = threading.Lock()

# Short-phrase WAV cache — pre-rendered at startup so common acks play instantly
_CACHED_PHRASES = [
    "Yes?", "Got it.", "Alright.", "Sure.", "Done.",
    "On it.", "Opening that now.", "Setting that up.",
    "I'm listening.", "Still here.", "Goodbye.",
    "Restarting now.", "One moment.", "Let me check.",
    "Good morning.", "Online.", "Ready.",
]
_phrase_cache: dict[str, str] = {}   # normalised text → tmp WAV path
_phrase_cache_lock = threading.Lock()

# ── Persistent daemon state ────────────────────────────────────────────────────
_daemon_proc: subprocess.Popen | None = None
_daemon_lock = threading.Lock()   # serialises requests + restart logic
_daemon_ready = False             # True once "{"ready": true}" received


# ── Path resolution ────────────────────────────────────────────────────────────

def _cache_key(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _resolve_paths() -> tuple[str | None, str | None]:
    """Return (venv_python, script_path) or (None, None)."""
    global _venv_python, _script_path, _paths_checked
    with _paths_lock:
        if _paths_checked:
            return _venv_python, _script_path
        _paths_checked = True

        # Locate tts_subprocess.py
        script_candidates = [
            Path(__file__).resolve().parent / "tts_subprocess.py",
            Path("/Users/truthseeker/jarvis-ai/local_runtime/tts_subprocess.py"),
        ]
        try:
            import sys
            if getattr(sys, "frozen", False):
                exe = Path(sys.executable).resolve()
                script_candidates.append(
                    exe.parent.parent / "Resources" / "local_runtime" / "tts_subprocess.py"
                )
        except Exception:
            pass

        found_script: str | None = None
        for c in script_candidates:
            if Path(c).exists():
                found_script = str(c)
                break

        if found_script is None:
            return None, None

        # Locate a Python that can import kokoro_onnx
        python_candidates = [
            "/Users/truthseeker/jarvis-ai/venv/bin/python3",
            str(Path(__file__).resolve().parent.parent / "venv" / "bin" / "python3"),
        ]
        try:
            import sys as _sys
            if getattr(_sys, "frozen", False):
                exe = Path(_sys.executable).resolve()
                python_candidates.append(
                    str(exe.parent.parent.parent / "venv" / "bin" / "python3")
                )
        except Exception:
            pass

        found_python: str | None = None
        for p in python_candidates:
            if not Path(p).exists():
                continue
            try:
                r = subprocess.run(
                    [p, "-c", "import kokoro_onnx; print('ok')"],
                    capture_output=True, timeout=8, text=True,
                )
                if r.returncode == 0 and "ok" in r.stdout:
                    found_python = p
                    break
            except Exception:
                continue

        _venv_python = found_python
        _script_path = found_script if found_python else None
        return _venv_python, _script_path


# ── Daemon lifecycle ───────────────────────────────────────────────────────────

def _daemon_alive() -> bool:
    """Return True if the daemon process is still running."""
    global _daemon_proc
    return _daemon_proc is not None and _daemon_proc.poll() is None


def _start_daemon() -> bool:
    """
    Start the persistent TTS daemon subprocess.
    Blocks until {"ready": true} is received or DAEMON_STARTUP_TIMEOUT elapses.
    Must be called with _daemon_lock held.
    Returns True on success.
    """
    global _daemon_proc, _daemon_ready

    python, script = _resolve_paths()
    if not python or not script:
        return False

    try:
        proc = subprocess.Popen(
            [python, script, "--daemon"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,           # line-buffered
        )
    except Exception as exc:
        import sys as _sys
        _sys.stderr.write(f"[KokoroDaemon] Failed to start: {exc}\n")
        return False

    # Wait for {"ready": true} line
    deadline = time.monotonic() + DAEMON_STARTUP_TIMEOUT
    ready = False
    while time.monotonic() < deadline:
        # Non-blocking readline via a short-lived reader thread
        line_holder: list[str] = []
        exc_holder: list[Exception] = []

        def _read_line():
            try:
                line_holder.append(proc.stdout.readline())
            except Exception as e:
                exc_holder.append(e)

        t = threading.Thread(target=_read_line, daemon=True)
        t.start()
        t.join(timeout=deadline - time.monotonic())

        if exc_holder or not line_holder:
            break  # process died or timed out

        raw = line_holder[0].strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
            if msg.get("ready"):
                ready = True
                break
            else:
                # Startup failure reported by daemon
                import sys as _sys
                _sys.stderr.write(f"[KokoroDaemon] Startup error: {msg.get('error')}\n")
                break
        except json.JSONDecodeError:
            continue  # ignore non-JSON stderr bleed

    if not ready:
        try:
            proc.kill()
        except Exception:
            pass
        return False

    _daemon_proc = proc
    _daemon_ready = True
    return True


def _ensure_daemon() -> bool:
    """
    Ensure the daemon is running, starting or restarting it if needed.
    Serialised via _daemon_lock.
    Returns True if daemon is ready to accept requests.
    """
    global _daemon_proc, _daemon_ready
    with _daemon_lock:
        if _daemon_alive() and _daemon_ready:
            return True
        # Clean up dead process
        if _daemon_proc is not None:
            try:
                _daemon_proc.kill()
            except Exception:
                pass
            _daemon_proc = None
            _daemon_ready = False
        return _start_daemon()


def _daemon_synthesize(text: str, voice: str) -> str | None:
    """
    Send a synthesis request to the persistent daemon and return the WAV path.
    Acquires _daemon_lock for the full round-trip to serialise concurrent callers.
    Falls back to None on any error (caller will use one-shot fallback).
    """
    global _daemon_proc, _daemon_ready
    with _daemon_lock:
        if not _daemon_alive() or not _daemon_ready:
            # Try to restart inline (cold-start penalty, but only on failure)
            _daemon_proc_local = _daemon_proc  # snapshot under lock
            if _daemon_proc_local is not None:
                try:
                    _daemon_proc_local.kill()
                except Exception:
                    pass
            _daemon_proc = None
            _daemon_ready = False
            if not _start_daemon():
                return None

        proc = _daemon_proc
        try:
            request = json.dumps({"text": text, "voice": voice}) + "\n"
            proc.stdin.write(request)
            proc.stdin.flush()
        except Exception:
            return None

        # Read response with timeout via a reader thread
        line_holder: list[str] = []

        def _read_line():
            try:
                line_holder.append(proc.stdout.readline())
            except Exception:
                pass

        t = threading.Thread(target=_read_line, daemon=True)
        t.start()
        t.join(timeout=DAEMON_REQUEST_TIMEOUT)

        if not line_holder:
            # Timed out or process died — mark daemon as dead for next call
            _daemon_ready = False
            return None

        raw = line_holder[0].strip()
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return data.get("wav_path") if data.get("ok") else None
        except json.JSONDecodeError:
            return None


def start_daemon_async() -> None:
    """
    Non-blocking daemon startup — called at prewarm time so the first real
    synthesis request finds the model already loaded.
    """
    def _start():
        _ensure_daemon()
    threading.Thread(target=_start, daemon=True, name="KokoroDaemonStart").start()


# ── One-shot synthesis (fallback) ─────────────────────────────────────────────

def _normalize_text(text: str) -> str:
    normalized = " ".join((text or "").split()).strip()
    return normalized[:MAX_TEXT_LENGTH] if len(normalized) > MAX_TEXT_LENGTH else normalized


def _run_synthesis_oneshot(text: str, voice: str) -> str | None:
    """Call tts_subprocess.py in legacy one-shot mode; return WAV path or None."""
    python, script = _resolve_paths()
    if not python or not script:
        return None
    try:
        result = subprocess.run(
            [python, script, text, voice],
            capture_output=True, timeout=SUBPROCESS_TIMEOUT, text=True,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        return data.get("wav_path") if data.get("ok") else None
    except Exception:
        return None


def _play_and_delete(wav_path: str) -> None:
    """Play WAV with afplay and always delete the temp file."""
    try:
        subprocess.run(["afplay", wav_path], check=True, capture_output=True, timeout=120)
    except Exception:
        pass
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass


# ── Public API (matches local_kokoro_tts interface) ───────────────────────────

def available() -> bool:
    python, script = _resolve_paths()
    return bool(JARVIS_KOKORO_TTS_ENABLED and python and script)


def config() -> dict[str, Any]:
    python, script = _resolve_paths()
    return {
        "engine": DEFAULT_ENGINE,
        "enabled": JARVIS_KOKORO_TTS_ENABLED,
        "voice": JARVIS_KOKORO_VOICE,
        "available": available(),
        "subprocess_mode": True,
        "daemon_mode": True,
        "daemon_alive": _daemon_alive() and _daemon_ready,
        "venv_python": python,
        "script": script,
    }


def prewarm_phrase_cache() -> None:
    """
    1. Start the persistent daemon so the model is in memory before first use.
    2. Pre-render short ack phrases via the daemon so they play instantly.
    """
    # Kick off daemon startup first (non-blocking)
    start_daemon_async()

    def _render():
        voice = JARVIS_KOKORO_VOICE
        # Wait briefly for daemon to be ready before rendering phrases
        deadline = time.monotonic() + DAEMON_STARTUP_TIMEOUT + 5
        while time.monotonic() < deadline:
            if _daemon_alive() and _daemon_ready:
                break
            time.sleep(0.5)

        for phrase in _CACHED_PHRASES:
            key = _cache_key(phrase)
            with _phrase_cache_lock:
                if key in _phrase_cache and Path(_phrase_cache[key]).exists():
                    continue
            wav = _daemon_synthesize(phrase, voice)
            if wav is None:
                # Daemon not ready yet — fall back to one-shot for phrase cache
                wav = _run_synthesis_oneshot(phrase, voice)
            if wav:
                with _phrase_cache_lock:
                    _phrase_cache[key] = wav

    threading.Thread(target=_render, daemon=True, name="KokoroPhraseCache").start()


def speak(text: str) -> dict[str, Any]:
    normalized = _normalize_text(text)
    voice = JARVIS_KOKORO_VOICE

    if not normalized:
        return {"ok": False, "engine": DEFAULT_ENGINE, "spoken": False,
                "text": "", "voice": voice, "error": "empty text"}

    if not JARVIS_KOKORO_TTS_ENABLED:
        return {"ok": False, "engine": DEFAULT_ENGINE, "spoken": False,
                "text": normalized, "voice": voice, "error": "kokoro TTS disabled"}

    # Cache hit — play pre-rendered phrase immediately
    key = _cache_key(normalized)
    with _phrase_cache_lock:
        cached = _phrase_cache.get(key)
    if cached and Path(cached).exists():
        try:
            _play_and_delete(cached)
            with _phrase_cache_lock:
                _phrase_cache.pop(key, None)   # consumed; re-render on next prewarm
            return {"ok": True, "engine": DEFAULT_ENGINE, "spoken": True,
                    "text": normalized, "voice": voice, "error": ""}
        except Exception:
            pass

    python, script = _resolve_paths()
    if not python:
        return {"ok": False, "engine": DEFAULT_ENGINE, "spoken": False,
                "text": normalized, "voice": voice,
                "error": "venv Python with kokoro-onnx not found"}
    if not script:
        return {"ok": False, "engine": DEFAULT_ENGINE, "spoken": False,
                "text": normalized, "voice": voice,
                "error": "tts_subprocess.py not found"}

    # Try daemon first (fast path — no model reload)
    wav = _daemon_synthesize(normalized, voice)

    # Fall back to one-shot subprocess if daemon unavailable
    if wav is None:
        wav = _run_synthesis_oneshot(normalized, voice)

    if not wav:
        return {"ok": False, "engine": DEFAULT_ENGINE, "spoken": False,
                "text": normalized, "voice": voice,
                "error": "subprocess synthesis failed"}

    _play_and_delete(wav)
    return {"ok": True, "engine": DEFAULT_ENGINE, "spoken": True,
            "text": normalized, "voice": voice, "error": ""}
