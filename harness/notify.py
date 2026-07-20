"""
harness/notify.py — Canonical macOS notification sender for Jarvis.

Single call-site: notify(title, body). Non-blocking (Popen), non-fatal.
Other modules should import from here rather than calling osascript directly.
"""
from __future__ import annotations

import logging
import platform
import subprocess


def notify(title: str, body: str, subtitle: str = "", sound: str = "Ping") -> None:
    """Send a macOS banner notification. Non-blocking, never raises."""
    system = platform.system()
    if system == "Darwin":
        _notify_macos(title, body, subtitle, sound)
    elif system == "Linux":
        _notify_linux(title, body)


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _notify_macos(title: str, body: str, subtitle: str, sound: str) -> None:
    subtitle_part = f'subtitle "{_escape(subtitle)}" ' if subtitle else ""
    script = (
        f'display notification "{_escape(body)}" '
        f'with title "{_escape(title)}" '
        f'{subtitle_part}'
        f'sound name "{sound}"'
    )
    try:
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        logging.debug("[notify] macOS notification failed", exc_info=True)


def _notify_linux(title: str, body: str) -> None:
    try:
        subprocess.Popen(
            ["notify-send", title, body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        logging.debug("[notify] Linux notification failed", exc_info=True)
