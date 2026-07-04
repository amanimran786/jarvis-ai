"""
harness/circuit_breaker.py — Persistent per-provider circuit breaker.

Complements the short in-memory cooldown in provider_priority.py with a
breaker that survives restarts. Real provider throttling lasts 10+ minutes;
the breaker stops Jarvis from hammering a dead provider across process
lifetimes.

States:
  CLOSED    — healthy, requests flow normally
  OPEN      — failing, skip the provider until the open window elapses
  HALF_OPEN — open window elapsed, allow one test request

Transitions:
  3 consecutive rate-limit failures  → OPEN for 10 minutes
  OPEN + 10 minutes elapsed          → HALF_OPEN (probe allowed)
  success in HALF_OPEN               → CLOSED
  failure in HALF_OPEN               → OPEN for another 10 minutes

State persists to logs/circuit_breaker.json. Provider health is mirrored
into ORCHESTRATOR_STATUS.json ("provider_health") on every state change so
the dashboard and autonomous agents can see throttled providers live.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

CLOSED = "CLOSED"
OPEN = "OPEN"
HALF_OPEN = "HALF_OPEN"

FAILURE_THRESHOLD = int(os.getenv("JARVIS_CIRCUIT_FAILURE_THRESHOLD", "3"))
OPEN_SECONDS = float(os.getenv("JARVIS_CIRCUIT_OPEN_SECONDS", "600"))

_lock = threading.Lock()
_states: dict[str, dict] | None = None  # provider -> state dict, lazy-loaded


# ── Paths (same packaged-app pattern as harness/budget.py) ────────────────────

def _base_dir() -> Path:
    try:
        import sys
        if getattr(sys, "frozen", False):
            import runtime_state
            return runtime_state.app_data_dir()
    except Exception:
        logging.debug("[CircuitBreaker] silent failure in _base_dir", exc_info=True)
    return Path(__file__).resolve().parent.parent


def _state_path() -> Path:
    override = os.getenv("JARVIS_CIRCUIT_BREAKER_PATH", "").strip()
    if override:
        return Path(override)
    return _base_dir() / "logs" / "circuit_breaker.json"


def _orchestrator_status_path() -> Path:
    override = os.getenv("JARVIS_ORCHESTRATOR_STATUS_PATH", "").strip()
    if override:
        return Path(override)
    return _base_dir() / "ORCHESTRATOR_STATUS.json"


# ── Persistence ───────────────────────────────────────────────────────────────

def _default_state() -> dict:
    return {
        "state": CLOSED,
        "failures": 0,
        "opened_at": None,   # epoch seconds when the breaker last opened
        "last_failure": None,
        "last_success": None,
    }


def _load_locked() -> dict[str, dict]:
    """Load persisted states. Caller must hold _lock."""
    global _states
    if _states is not None:
        return _states
    _states = {}
    try:
        path = _state_path()
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for provider, data in raw.items():
                    if isinstance(data, dict):
                        merged = _default_state()
                        merged.update(data)
                        _states[provider] = merged
    except Exception:
        log.warning("[CircuitBreaker] failed to load state file — starting fresh", exc_info=True)
        _states = {}
    return _states


def _save_locked() -> None:
    """Persist states atomically. Caller must hold _lock."""
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(json.dumps(_states or {}, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        log.warning("[CircuitBreaker] failed to persist state", exc_info=True)


def _entry_locked(provider: str) -> dict:
    states = _load_locked()
    if provider not in states:
        states[provider] = _default_state()
    return states[provider]


def _iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def _maybe_half_open_locked(provider: str, entry: dict) -> None:
    """OPEN → HALF_OPEN once the open window elapses. Caller must hold _lock."""
    if entry["state"] == OPEN:
        opened_at = float(entry.get("opened_at") or 0.0)
        if time.time() - opened_at >= OPEN_SECONDS:
            entry["state"] = HALF_OPEN
            log.info("[CircuitBreaker] %s: OPEN window elapsed — HALF_OPEN (probing)", provider)
            _save_locked()


# ── Public API ────────────────────────────────────────────────────────────────

def record_failure(provider: str) -> None:
    """Record a rate-limit failure for a provider. Opens the breaker after
    FAILURE_THRESHOLD consecutive failures, or immediately on a HALF_OPEN probe."""
    now = time.time()
    with _lock:
        entry = _entry_locked(provider)
        entry["failures"] = int(entry.get("failures", 0)) + 1
        entry["last_failure"] = now
        if entry["state"] == HALF_OPEN:
            entry["state"] = OPEN
            entry["opened_at"] = now
            log.warning(
                "[CircuitBreaker] %s: HALF_OPEN probe failed — OPEN for %.0fs",
                provider, OPEN_SECONDS,
            )
        elif entry["state"] == CLOSED and entry["failures"] >= FAILURE_THRESHOLD:
            entry["state"] = OPEN
            entry["opened_at"] = now
            log.warning(
                "[CircuitBreaker] %s: %d consecutive rate-limit failures — OPEN for %.0fs",
                provider, entry["failures"], OPEN_SECONDS,
            )
        _save_locked()
    write_status_snapshot()


def record_success(provider: str) -> None:
    """Record a successful response. Closes the breaker and resets failures."""
    with _lock:
        entry = _entry_locked(provider)
        changed = entry["state"] != CLOSED or entry.get("failures", 0) != 0
        if not changed:
            # Update last_success in memory only — avoid a disk write per request.
            entry["last_success"] = time.time()
            return
        if entry["state"] != CLOSED:
            log.info("[CircuitBreaker] %s: success — CLOSED (recovered)", provider)
        entry["state"] = CLOSED
        entry["failures"] = 0
        entry["opened_at"] = None
        entry["last_success"] = time.time()
        _save_locked()
    write_status_snapshot()


def is_available(provider: str) -> bool:
    """False while the breaker is OPEN. HALF_OPEN allows a probe request."""
    with _lock:
        entry = _entry_locked(provider)
        _maybe_half_open_locked(provider, entry)
        return entry["state"] != OPEN


def get_state(provider: str) -> dict:
    """Current breaker state for dashboards/logging."""
    with _lock:
        entry = _entry_locked(provider)
        _maybe_half_open_locked(provider, entry)
        snapshot = dict(entry)
    snapshot["provider"] = provider
    snapshot["until"] = _iso(
        (snapshot.get("opened_at") or 0) + OPEN_SECONDS
    ) if snapshot["state"] in (OPEN, HALF_OPEN) and snapshot.get("opened_at") else None
    return snapshot


def reset(provider: str | None = None) -> None:
    """Clear breaker state (all providers when provider is None). For tests."""
    global _states
    with _lock:
        states = _load_locked()
        if provider is None:
            states.clear()
        else:
            states.pop(provider, None)
        _save_locked()


def write_status_snapshot() -> None:
    """Mirror provider health into ORCHESTRATOR_STATUS.json ("provider_health").

    Best-effort: preserves every other key in the file and never raises.
    """
    with _lock:
        states = _load_locked()
        health: dict[str, dict] = {}
        for provider, entry in sorted(states.items()):
            item: dict = {
                "state": entry["state"],
                "failures": int(entry.get("failures", 0)),
            }
            if entry["state"] in (OPEN, HALF_OPEN) and entry.get("opened_at"):
                item["until"] = _iso(float(entry["opened_at"]) + OPEN_SECONDS)
            else:
                item["last_failure"] = _iso(entry.get("last_failure"))
            health[provider] = item
    try:
        path = _orchestrator_status_path()
        status: dict = {}
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                status = loaded
        status["provider_health"] = health
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(json.dumps(status, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        log.warning("[CircuitBreaker] failed to write ORCHESTRATOR_STATUS snapshot", exc_info=True)


# ── Shared rate-limit detection ───────────────────────────────────────────────

_RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "quota",
    "overloaded",
    "too many requests",
    "resource_exhausted",
)


def is_rate_limit_error(exc: Exception) -> bool:
    haystack = f"{type(exc).__name__} {exc}".lower()
    return any(marker in haystack for marker in _RATE_LIMIT_MARKERS)
