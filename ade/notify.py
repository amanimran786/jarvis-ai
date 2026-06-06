"""
ade/notify.py — System-native notifications (macOS osascript / Linux notify-send).
"""
from __future__ import annotations

import platform
import subprocess


def beep() -> None:
    print("\a", end="", flush=True)


def send(title: str, message: str, sound: str = "Ping") -> None:
    """Send a system notification and beep. Non-fatal on failure."""
    beep()
    system = platform.system()
    if system == "Darwin":
        script = (
            f'display notification {_osa_str(message)} '
            f'with title {_osa_str(title)} '
            f'sound name "{sound}"'
        )
        _run_silent(["osascript", "-e", script])
    elif system == "Linux":
        _run_silent(["notify-send", title, message])


def _osa_str(s: str) -> str:
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _run_silent(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, capture_output=True, timeout=5, check=False)
    except Exception:
        pass
