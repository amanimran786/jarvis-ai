"""Non-fatal spoken progress announcements for operative tasks."""

from __future__ import annotations

import logging

from local_runtime import local_tts


def speak_step(step_number: int, description: str, *, ok: bool) -> bool:
    """Speak a completed step through the existing macOS ``say`` backend."""
    status = "Completed" if ok else "Failed"
    utterance = f"{status} step {step_number}: {description}"

    try:
        result = local_tts.speak(utterance)
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "macOS say failed")
        return True
    except Exception:
        logging.exception("Operative step TTS failed; continuing without audio")
        return False
