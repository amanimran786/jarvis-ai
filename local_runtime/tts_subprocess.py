#!/usr/bin/env python3
"""
Standalone Kokoro TTS synthesizer — runs via venv Python from frozen .app.

One-shot usage (legacy, kept as fallback):
  venv/bin/python local_runtime/tts_subprocess.py "hello world" am_onyx
  → stdout: {"ok": true, "wav_path": "/tmp/kokoro_xyz.wav"}

Persistent daemon usage (eliminates cold-start latency):
  venv/bin/python local_runtime/tts_subprocess.py --daemon
  → reads JSON lines from stdin: {"text": "...", "voice": "..."}
  → writes JSON lines to stdout: {"ok": true, "wav_path": "..."} or {"ok": false, "error": "..."}
  → model is loaded once at startup and reused for every request
"""
import sys
import json
import tempfile
import os
from pathlib import Path


def _ensure_model_files() -> bool:
    model_dir = Path.home() / ".jarvis" / "kokoro"
    model_path = model_dir / "kokoro-v1.0.onnx"
    voices_path = model_dir / "voices-v1.0.bin"
    if model_path.exists() and voices_path.exists():
        return True
    model_dir.mkdir(parents=True, exist_ok=True)
    try:
        import urllib.request
        urls = {
            "model": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
            "voices": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
        }
        if not model_path.exists():
            sys.stderr.write("[Kokoro] Downloading model (~310MB)...\n"); sys.stderr.flush()
            urllib.request.urlretrieve(urls["model"], str(model_path))
        if not voices_path.exists():
            sys.stderr.write("[Kokoro] Downloading voices (~27MB)...\n"); sys.stderr.flush()
            urllib.request.urlretrieve(urls["voices"], str(voices_path))
        return True
    except Exception as exc:
        sys.stderr.write(f"[Kokoro] Download failed: {exc}\n"); sys.stderr.flush()
        return False


def _synthesize_with_model(kokoro_instance, text: str, voice: str) -> str | None:
    """Synthesize using an already-loaded Kokoro instance."""
    try:
        import numpy as np, wave, io
        samples, sr = kokoro_instance.create(text.strip(), voice=voice, speed=1.0, lang="en-us")
        samples_i16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sr)
            wf.writeframes(samples_i16.tobytes())
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(buf.getvalue())
            return f.name
    except Exception as exc:
        sys.stderr.write(f"[Kokoro] Synthesis error: {exc}\n"); sys.stderr.flush()
        return None


def _synthesize(text: str, voice: str) -> str | None:
    """One-shot synthesis: load model, synthesize, return WAV path."""
    if not _ensure_model_files():
        return None
    try:
        from kokoro_onnx import Kokoro
        model_dir = Path.home() / ".jarvis" / "kokoro"
        k = Kokoro(str(model_dir / "kokoro-v1.0.onnx"), str(model_dir / "voices-v1.0.bin"))
        return _synthesize_with_model(k, text, voice)
    except Exception as exc:
        sys.stderr.write(f"[Kokoro] Load/synthesis error: {exc}\n"); sys.stderr.flush()
        return None


def _reply(obj: dict) -> None:
    """Write a JSON line to stdout and flush immediately."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def run_daemon() -> None:
    """
    Persistent daemon mode.

    1. Load the ONNX model once.
    2. Signal readiness by writing {"ready": true} to stdout.
    3. Loop: read one JSON line from stdin, synthesize, write result to stdout.
    4. Exit cleanly on EOF (stdin closed).
    """
    if not _ensure_model_files():
        _reply({"ready": False, "error": "model files missing or download failed"})
        sys.exit(1)

    try:
        from kokoro_onnx import Kokoro
        model_dir = Path.home() / ".jarvis" / "kokoro"
        kokoro = Kokoro(
            str(model_dir / "kokoro-v1.0.onnx"),
            str(model_dir / "voices-v1.0.bin"),
        )
    except Exception as exc:
        _reply({"ready": False, "error": f"model load failed: {exc}"})
        sys.exit(1)

    # Signal that the daemon is ready to accept requests
    _reply({"ready": True})

    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            req = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            _reply({"ok": False, "error": f"invalid JSON: {exc}", "wav_path": None})
            continue

        text = req.get("text", "").strip()
        voice = req.get("voice", "am_onyx")

        if not text:
            _reply({"ok": False, "error": "empty text", "wav_path": None})
            continue

        wav_path = _synthesize_with_model(kokoro, text, voice)
        if wav_path:
            _reply({"ok": True, "error": "", "wav_path": wav_path})
        else:
            _reply({"ok": False, "error": "synthesis failed", "wav_path": None})


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--daemon":
        run_daemon()
        return

    # ── One-shot legacy mode ──────────────────────────────────────────────────
    if len(sys.argv) < 3:
        print(json.dumps({"ok": False, "error": "usage: tts_subprocess.py TEXT VOICE", "wav_path": None}))
        sys.exit(1)

    text = sys.argv[1]
    voice = sys.argv[2]

    if not text.strip():
        print(json.dumps({"ok": False, "error": "empty text", "wav_path": None}))
        sys.exit(1)

    wav_path = _synthesize(text, voice)
    if wav_path:
        print(json.dumps({"ok": True, "error": "", "wav_path": wav_path}))
    else:
        print(json.dumps({"ok": False, "error": "synthesis failed", "wav_path": None}))
        sys.exit(1)


if __name__ == "__main__":
    main()
