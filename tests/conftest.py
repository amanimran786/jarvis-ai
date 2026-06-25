"""
tests/conftest.py — Session-wide pre-imports and sys.modules guards.

Modules listed in _EARLY_IMPORTS are imported here (with the real config
in place) before any test file runs its module-level code that may
replace sys.modules["config"] with a MagicMock.  This ensures that
module-level constants (e.g. APPLE_FOUNDATION_BASE_URL) are bound to real
values and not to MagicMock objects.
"""
import logging
import os
import sys

# ── CI stubs — pre-stub macOS-only and hardware packages before any import ────
# GitHub Actions always sets CI=true. These are all mocked in individual tests;
# stubbing them here prevents collection-time ImportError on Linux runners.
if os.getenv("CI"):
    from unittest.mock import MagicMock as _MM

    _CI_STUBS = [
        # macOS Objective-C bridge
        "objc", "PyObjCTools", "PyObjCTools.AppHelper",
        "AppKit", "Foundation", "Quartz",
        "Cocoa", "CoreFoundation", "CoreServices",
        # macOS-only GUI
        "PyQt6", "PyQt6.QtWidgets", "PyQt6.QtCore", "PyQt6.QtGui",
        "PyQt6.QtMultimedia", "PyQt6.QtSvg",
        # macOS Vision framework (used by local_ocr)
        "Vision", "CoreML",
        # Audio hardware (PortAudio-backed)
        "sounddevice", "pyaudio",
        # Local ML models (large, not installed in CI)
        "kokoro_onnx", "kokoro", "faster_whisper",
        # Optional vision
        "cv2",
        # Optional TTS engine
        "pyttsx3",
    ]
    for _s in _CI_STUBS:
        if _s not in sys.modules:
            sys.modules[_s] = _MM()

# Unit tests must never make real verifier LLM calls. Background task threads
# run _auto_verify after _complete_task; without this, tests with a mocked
# smart_stream still leak real GPT_MINI calls (and pollute the verdict log).
os.environ.setdefault("JARVIS_AUTO_VERIFY", "0")

# Unit tests mock smart_stream, not _run_native_task_loop. Disabling the native
# loop here routes all task execution through the smart_stream mock path so
# existing tests continue to work without modification.
os.environ.setdefault("JARVIS_NATIVE_TOOL_LOOP", "0")

# Keep test-generated security audit entries out of the real operator ledger
# (~/.jarvis/security_audit.jsonl) — approve/deny/verdict paths emit on call.
import tempfile as _tempfile  # noqa: E402
os.environ.setdefault(
    "JARVIS_SECURITY_AUDIT_PATH",
    os.path.join(_tempfile.mkdtemp(prefix="jarvis_test_audit_"), "security_audit.jsonl"),
)

# Pin the task DB to a throwaway file for the whole session. task_persistence.db_path()
# checks JARVIS_TASK_DB_PATH first, so this short-circuits the runtime_state fallback:
# tests that stub runtime_state with a MagicMock can no longer leak a stray
# "<MagicMock name='mock.writable_data_path()' ...>" sqlite file into the repo root,
# and no test writes the real task DB. Per-test patch.dict overrides still win.
os.environ.setdefault(
    "JARVIS_TASK_DB_PATH",
    os.path.join(_tempfile.mkdtemp(prefix="jarvis_test_taskdb_"), "jarvis_tasks.sqlite3"),
)

_EARLY_IMPORTS = [
    "tools",               # must be first: prevents test_backend_engineer from seeing _real_tools=None
    "brains.brain_apple_foundation",
    "brains.brain_ollama",
]

for _mod in _EARLY_IMPORTS:
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except Exception:
            logging.debug("[Conftest] silent failure in unknown", exc_info=True)

