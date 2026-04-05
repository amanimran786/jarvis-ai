"""
Makes the Jarvis window invisible to screen sharing tools (Zoom, Teams, etc.)
Uses macOS NSWindowSharingNone via pyobjc.
"""


def apply_stealth(win_id: int) -> None:
    """Set NSWindowSharingNone on the specific window matching win_id."""
    try:
        import objc
        from AppKit import NSApp
        from Cocoa import NSWindow

        # NSWindowSharingNone = 0
        # Only hide the specific window, not every window in the app.
        # Applying it to all windows can cause the meeting overlay to go black.
        for ns_window in NSApp.windows():
            if ns_window.windowNumber() == win_id:
                ns_window.setSharingType_(0)
                print(f"[Stealth] Window {win_id} hidden from screen sharing.")
                return
        # Fallback: hide all if window number not matched
        for ns_window in NSApp.windows():
            ns_window.setSharingType_(0)
        print("[Stealth] All windows hidden from screen sharing (fallback).")
    except Exception as e:
        print(f"[Stealth] Could not apply stealth mode: {e}")
