"""Runtime control for whether Jarvis windows appear in screen sharing.

Default is undetectable for privacy. The UI can switch to detectable mode
temporarily when Aman needs screenshots or screen-share debugging.
"""

_enabled = True


def is_enabled() -> bool:
    return _enabled


def set_enabled(enabled: bool) -> bool:
    global _enabled
    _enabled = bool(enabled)
    apply_current_mode()
    return _enabled


def toggle_enabled() -> bool:
    return set_enabled(not _enabled)


def snapshot() -> dict:
    return {
        "enabled": _enabled,
        "mode": "undetectable" if _enabled else "detectable",
    }


def status_text() -> str:
    if _enabled:
        return "Visibility: undetectable. Jarvis is hidden from screen sharing."
    return "Visibility: detectable. Jarvis can be captured for debugging."


def _sharing_type() -> int:
    # NSWindowSharingNone = 0, NSWindowSharingReadOnly = 1.
    return 0 if _enabled else 1


def apply_current_mode(win_id: int = 0) -> None:
    """Apply the current sharing mode to all app windows, or one window id."""
    try:
        from AppKit import NSApp
        for ns_window in NSApp.windows():
            if win_id and ns_window.windowNumber() != win_id:
                continue
            ns_window.setSharingType_(_sharing_type())
    except Exception as e:
        print(f"[Stealth] Could not apply visibility mode: {e}")


def apply_stealth(win_id: int = 0) -> None:
    """Compatibility wrapper: apply the currently selected visibility mode."""
    apply_current_mode(win_id)
