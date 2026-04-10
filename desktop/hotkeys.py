"""
Global hotkey system for Jarvis.
Works system-wide — even during Zoom/Teams calls with screen share active.
The Jarvis window is already invisible to screen share (NSWindowSharingNone).

Default hotkeys:
  Cmd + Shift + J  →  Capture full screen → Jarvis analyzes it silently
  Cmd + Shift + K  →  Capture webcam frame → Jarvis analyzes it
  Cmd + Shift + L  →  Read clipboard → Jarvis responds
  Cmd + Shift + ;  →  Toggle Jarvis window visibility (show/hide)
"""

import threading
import os
import platform
from pynput import keyboard

# Callbacks set by ui.py
_on_screen  = None
_on_webcam  = None
_on_clip    = None
_on_toggle  = None
_on_listen  = None
_on_overlay = None

_current_keys = set()

HOTKEYS = {
    frozenset([keyboard.Key.cmd, keyboard.Key.alt, keyboard.KeyCode.from_char('j')]): 'screen',
    frozenset([keyboard.Key.cmd, keyboard.Key.alt, keyboard.KeyCode.from_char('k')]): 'webcam',
    frozenset([keyboard.Key.cmd, keyboard.Key.alt, keyboard.KeyCode.from_char('l')]): 'clip',
    frozenset([keyboard.Key.cmd, keyboard.Key.alt, keyboard.KeyCode.from_char(';')]): 'toggle',
    frozenset([keyboard.Key.cmd, keyboard.Key.alt, keyboard.KeyCode.from_char('m')]): 'listen',
    frozenset([keyboard.Key.cmd, keyboard.Key.alt, keyboard.KeyCode.from_char('o')]): 'overlay',
}


def register(on_screen=None, on_webcam=None, on_clip=None, on_toggle=None, on_listen=None, on_overlay=None):
    global _on_screen, _on_webcam, _on_clip, _on_toggle, _on_listen, _on_overlay
    _on_screen  = on_screen
    _on_webcam  = on_webcam
    _on_clip    = on_clip
    _on_toggle  = on_toggle
    _on_listen  = on_listen
    _on_overlay = on_overlay


def _normalize(key):
    """Normalize key for consistent matching."""
    try:
        if hasattr(key, 'char') and key.char:
            return keyboard.KeyCode.from_char(key.char.lower())
    except Exception:
        pass
    return key


def _on_press(key):
    _current_keys.add(_normalize(key))
    for combo, action in HOTKEYS.items():
        if combo.issubset(_current_keys):
            _fire(action)


def _on_release(key):
    _current_keys.discard(_normalize(key))


def _fire(action: str):
    cb = {'screen': _on_screen, 'webcam': _on_webcam,
          'clip': _on_clip, 'toggle': _on_toggle,
          'listen': _on_listen, 'overlay': _on_overlay}.get(action)
    if cb:
        threading.Thread(target=cb, daemon=True).start()


def start():
    """Start the global hotkey listener in a background thread."""
    if platform.system() == "Darwin" and os.getenv("JARVIS_ENABLE_GLOBAL_HOTKEYS", "").lower() not in {"1", "true", "yes", "on"}:
        print("[Hotkeys] Disabled on macOS by default for stability. Set JARVIS_ENABLE_GLOBAL_HOTKEYS=1 to re-enable.")
        return None
    listener = keyboard.Listener(on_press=_on_press, on_release=_on_release)
    listener.daemon = True
    listener.start()
    print("[Hotkeys] Active — Cmd+Opt+J: screen | K: webcam | L: clipboard | M: listen | ;: toggle | O: overlay")
    return listener
