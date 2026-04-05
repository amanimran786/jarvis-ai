"""
Makes Jarvis windows invisible to screen sharing tools (Zoom, Teams, etc.)
Uses macOS NSWindowSharingNone via pyobjc.
"""


def apply_stealth(win_id: int = 0) -> None:
    """Set NSWindowSharingNone on all app windows so they won't appear in screen shares."""
    try:
        from AppKit import NSApp
        for ns_window in NSApp.windows():
            ns_window.setSharingType_(0)
    except Exception as e:
        print(f"[Stealth] Could not apply stealth mode: {e}")
