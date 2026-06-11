"""
tests/conftest.py — Session-wide pre-imports and sys.modules guards.

Modules listed in _EARLY_IMPORTS are imported here (with the real config
in place) before any test file runs its module-level code that may
replace sys.modules["config"] with a MagicMock.  This ensures that
module-level constants (e.g. APPLE_FOUNDATION_BASE_URL) are bound to real
values and not to MagicMock objects.
"""
import os
import sys

# Unit tests must never make real verifier LLM calls. Background task threads
# run _auto_verify after _complete_task; without this, tests with a mocked
# smart_stream still leak real GPT_MINI calls (and pollute the verdict log).
os.environ.setdefault("JARVIS_AUTO_VERIFY", "0")

_EARLY_IMPORTS = [
    "brains.brain_apple_foundation",
    "brains.brain_ollama",
]

for _mod in _EARLY_IMPORTS:
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except Exception:
            pass
