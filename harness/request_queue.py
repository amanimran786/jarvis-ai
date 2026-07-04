"""
harness/request_queue.py — Request queue for total provider exhaustion.

Holds LLM requests when ALL providers are temporarily unavailable (rate-limited
or down) instead of failing hard. Drains automatically as providers recover
(circuit breakers close). Complements harness/circuit_breaker.py: the breaker
says "stop calling this provider"; the queue says "park the request and retry
when someone recovers".

Callers block inside enqueue() until their request executes or the wait budget
elapses. Queue depth is mirrored into ORCHESTRATOR_STATUS.json ("queue_depth",
"queue_oldest_wait_seconds") whenever it changes so the dashboard can show it.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

# Config (read from env, fall back to defaults)
QUEUE_ENABLED = os.getenv("JARVIS_QUEUE_ON_EXHAUSTION", "1") == "1"
QUEUE_MAX_DEPTH = int(os.getenv("JARVIS_QUEUE_MAX_DEPTH", "10"))
QUEUE_MAX_WAIT_SECONDS = int(os.getenv("JARVIS_QUEUE_MAX_WAIT_SECONDS", "60"))
DRAIN_INTERVAL_SECONDS = 5.0  # how often to check if providers recovered

# Providers the drain loop probes via the circuit breaker.
_PROVIDERS = ("openai", "gemini", "anthropic")


class QueueFullError(RuntimeError):
    """Raised when the request queue is at max depth."""


class QueueTimeoutError(RuntimeError):
    """Raised when a queued request waits longer than QUEUE_MAX_WAIT_SECONDS."""


@dataclass
class QueuedRequest:
    fn: Callable
    args: tuple
    kwargs: dict
    result: queue.Queue  # single-item queue; ("ok", value) or ("err", exc)
    enqueued_at: float   # time.monotonic()
    request_id: str      # uuid for logging
    cancelled: threading.Event = field(default_factory=threading.Event)


_lock = threading.Lock()
_pending: list[QueuedRequest] = []
_drain_thread: threading.Thread | None = None


# ── Status file mirroring (same packaged-app pattern as circuit_breaker) ──────

def _base_dir() -> Path:
    try:
        import sys
        if getattr(sys, "frozen", False):
            import runtime_state
            return runtime_state.app_data_dir()
    except Exception:
        logging.debug("[RequestQueue] silent failure in _base_dir", exc_info=True)
    return Path(__file__).resolve().parent.parent


def _orchestrator_status_path() -> Path:
    override = os.getenv("JARVIS_ORCHESTRATOR_STATUS_PATH", "").strip()
    if override:
        return Path(override)
    return _base_dir() / "ORCHESTRATOR_STATUS.json"


def _write_status_snapshot() -> None:
    """Mirror queue depth into ORCHESTRATOR_STATUS.json. Best-effort, never raises."""
    with _lock:
        depth = len(_pending)
        oldest_wait = 0.0
        if _pending:
            oldest_wait = max(0.0, time.monotonic() - _pending[0].enqueued_at)
    try:
        path = _orchestrator_status_path()
        status: dict = {}
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                status = loaded
        status["queue_depth"] = depth
        status["queue_oldest_wait_seconds"] = round(oldest_wait, 1)
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(json.dumps(status, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        log.warning("[RequestQueue] failed to write ORCHESTRATOR_STATUS snapshot", exc_info=True)


# ── Public API ────────────────────────────────────────────────────────────────

def queue_depth() -> int:
    """Current number of parked requests, for status reporting."""
    with _lock:
        return len(_pending)


def should_queue() -> bool:
    """True when exhausted requests should be parked instead of raised."""
    return QUEUE_ENABLED and queue_depth() < QUEUE_MAX_DEPTH


def enqueue(fn: Callable, *args: Any, **kwargs: Any) -> Any:
    """Park a request until a provider recovers, then execute it.

    Blocks the caller until the drain loop runs fn(*args, **kwargs) and a
    result is ready. Raises QueueFullError at max depth and QueueTimeoutError
    when the wait exceeds QUEUE_MAX_WAIT_SECONDS. Exceptions raised by fn
    propagate to the caller unchanged.
    """
    if not QUEUE_ENABLED:
        raise QueueFullError("Request queue is disabled (JARVIS_QUEUE_ON_EXHAUSTION=0)")

    request = QueuedRequest(
        fn=fn,
        args=args,
        kwargs=kwargs,
        result=queue.Queue(maxsize=1),
        enqueued_at=time.monotonic(),
        request_id=uuid.uuid4().hex[:12],
    )
    with _lock:
        if len(_pending) >= QUEUE_MAX_DEPTH:
            raise QueueFullError(
                f"Request queue full ({len(_pending)}/{QUEUE_MAX_DEPTH})"
            )
        _pending.append(request)
    log.info(
        "[RequestQueue] parked request %s (depth=%d) — waiting for provider recovery",
        request.request_id, queue_depth(),
    )
    _write_status_snapshot()
    start_drain_thread()

    try:
        outcome, payload = request.result.get(timeout=QUEUE_MAX_WAIT_SECONDS)
    except queue.Empty:
        request.cancelled.set()
        with _lock:
            if request in _pending:
                _pending.remove(request)
        _write_status_snapshot()
        raise QueueTimeoutError(
            f"Queued request {request.request_id} waited over "
            f"{QUEUE_MAX_WAIT_SECONDS}s for a provider to recover"
        ) from None
    if outcome == "err":
        raise payload
    return payload


def start_drain_thread() -> None:
    """Start the background drain daemon once (idempotent)."""
    global _drain_thread
    with _lock:
        if _drain_thread is not None and _drain_thread.is_alive():
            return
        _drain_thread = threading.Thread(
            target=_drain_loop, name="request-queue-drain", daemon=True
        )
        _drain_thread.start()


# ── Drain loop ────────────────────────────────────────────────────────────────

def _any_provider_available() -> bool:
    """True if the circuit breaker allows at least one cloud provider."""
    try:
        from harness import circuit_breaker
    except Exception:
        return True  # no breaker — nothing blocks a retry
    try:
        return any(circuit_breaker.is_available(p) for p in _PROVIDERS)
    except Exception:
        log.debug("[RequestQueue] circuit breaker check failed", exc_info=True)
        return True


def _pop_oldest() -> QueuedRequest | None:
    """Pop the oldest live request, discarding cancelled ones. Never blocks."""
    with _lock:
        while _pending:
            request = _pending.pop(0)
            if not request.cancelled.is_set():
                return request
    return None


def _drain_once() -> bool:
    """Execute the oldest queued request if a provider is available.

    Returns True when a request was executed (or discarded), False when the
    queue is empty or no provider has recovered yet.
    """
    if queue_depth() == 0:
        return False
    if not _any_provider_available():
        return False
    request = _pop_oldest()
    if request is None:
        return False
    _write_status_snapshot()
    waited = time.monotonic() - request.enqueued_at
    log.info(
        "[RequestQueue] draining request %s after %.1fs wait (depth=%d)",
        request.request_id, waited, queue_depth(),
    )
    try:
        value = request.fn(*request.args, **request.kwargs)
        outcome: tuple = ("ok", value)
    except Exception as exc:
        logging.exception("[RequestQueue] queued request %s failed", request.request_id)
        outcome = ("err", exc)
    try:
        request.result.put_nowait(outcome)
    except queue.Full:
        log.debug("[RequestQueue] result box already full for %s", request.request_id)
    return True


def _drain_loop() -> None:
    """Background daemon: every DRAIN_INTERVAL_SECONDS, drain if providers recovered."""
    while True:
        try:
            drained = _drain_once()
        except Exception:
            logging.exception("[RequestQueue] drain iteration failed")
            drained = False
        if not drained:
            time.sleep(DRAIN_INTERVAL_SECONDS)


def reset() -> None:
    """Clear queue state (cancels all pending requests). For tests."""
    with _lock:
        for request in _pending:
            request.cancelled.set()
        _pending.clear()
    _write_status_snapshot()
