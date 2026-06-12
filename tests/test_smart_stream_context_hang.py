"""smart_stream must not block forever when a context source hangs.

Live incident 2026-06-10: an agent task sat in "streaming" for 50+ minutes
because a context getter hung on a socket with no timeout. The per-future
result(timeout=...) guards fired, but the ThreadPoolExecutor `with` block
joined the hung worker on exit, blocking the request thread indefinitely.
"""

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import model_router


class ContextAssemblyHangTests(unittest.TestCase):
    def test_hung_semantic_memory_does_not_block_smart_stream(self):
        release = threading.Event()

        def hung_retrieve(*_a, **_kw):
            release.wait(timeout=60)
            return []

        start = time.monotonic()
        try:
            with patch.object(model_router._smem, "retrieve", side_effect=hung_retrieve), \
                 patch.object(model_router, "_current_mode", "auto"), \
                 patch.object(model_router, "_is_mobile_web_active", return_value=False), \
                 patch.object(model_router, "forced_model_status", return_value={"active": False}), \
                 patch.object(model_router, "_has_local", return_value=True), \
                 patch.object(model_router, "_best_local", return_value="jarvis-local"):
                # Stream is lazy — not consumed, so no model call happens.
                model_router.smart_stream("quick check", tool="chat", prefer_local=True)
        finally:
            release.set()
        elapsed = time.monotonic() - start
        # smem result timeout is 4s; vault/graph/mem0 are fast. Anything under
        # ~20s proves the executor exit no longer joins the hung worker.
        self.assertLess(elapsed, 20.0)


class ContextSkipTests(unittest.TestCase):
    """Tool-loop continuations skip dynamic retrieval (tokens + embed call)."""

    def _retrieve_calls(self, skip: bool) -> int:
        calls = []

        def spy_retrieve(*a, **kw):
            calls.append(1)
            return []

        with patch.object(model_router._smem, "retrieve", side_effect=spy_retrieve), \
             patch.object(model_router, "_current_mode", "auto"), \
             patch.object(model_router, "_is_mobile_web_active", return_value=False), \
             patch.object(model_router, "forced_model_status", return_value={"active": False}), \
             patch.object(model_router, "_has_local", return_value=True), \
             patch.object(model_router, "_best_local", return_value="jarvis-local"):
            model_router.smart_stream(
                "continue the task", tool="chat",
                prefer_local=True, skip_dynamic_context=skip,
            )
        return len(calls)

    def test_skip_dynamic_context_skips_semantic_retrieval(self):
        self.assertEqual(self._retrieve_calls(skip=True), 0)

    def test_default_still_retrieves(self):
        self.assertGreaterEqual(self._retrieve_calls(skip=False), 1)


if __name__ == "__main__":
    unittest.main()
