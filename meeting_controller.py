from __future__ import annotations

from typing import Any, Callable

import meeting_listener
import overlay


def current_meeting_label(force_refresh: bool = False) -> str | None:
    label = overlay.detect_meeting_app(force_refresh=force_refresh)
    return label or None


def refresh_status(force_refresh: bool = False) -> dict[str, Any]:
    meeting = current_meeting_label(force_refresh=force_refresh)
    snapshot = meeting_listener.status_snapshot()
    return {
        "meeting_label": meeting,
        "meeting_detected": meeting is not None,
        "listener": snapshot,
        "running": bool(snapshot.get("running", False)),
        "active_source": snapshot.get("active_source", {}),
        "preferred_source": snapshot.get("preferred", {}),
    }


def start_listening(
    on_transcript: Callable[[str], None] | None = None,
    on_suggestion: Callable[[str], None] | None = None,
) -> str:
    return meeting_listener.start(
        on_transcript=on_transcript,
        on_suggestion=on_suggestion,
    )


def stop_listening() -> str:
    return meeting_listener.stop()


def is_running() -> bool:
    return meeting_listener.is_running()


def listener_snapshot() -> dict[str, Any]:
    return meeting_listener.status_snapshot()

