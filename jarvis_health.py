"""
jarvis_health.py — Component health monitor for Iron Man Jarvis.

Checks all key subsystems and surfaces degraded components proactively.
Used by the watcher (periodic background checks) and by the verbal
"health check" / "system status" command.

Components checked:
  ollama      — is Ollama running, how many models available
  stt         — faster-whisper backend status
  tts         — Kokoro + say fallback status
  google      — Calendar / Gmail auth validity
  mem0        — Qdrant + episodic memory availability
  vault       — vault directory readable, index fresh
  watcher     — background watcher thread alive

Each check returns a ComponentStatus:
  { "name": str, "ok": bool, "detail": str, "degraded": bool }

Public API
──────────
  check_all()      -> dict[str, ComponentStatus]   Full health snapshot
  health_summary() -> str                          Human-readable one-liner per component
  degraded()       -> list[str]                    Names of degraded components
"""

from __future__ import annotations

import threading
import time
from typing import TypedDict


class ComponentStatus(TypedDict):
    name:     str
    ok:       bool
    detail:   str
    degraded: bool


# ── Individual checkers ───────────────────────────────────────────────────────

def _check_ollama() -> ComponentStatus:
    try:
        from brains import brain_ollama
        models = brain_ollama.list_local_models()
        if not models:
            return {"name": "ollama", "ok": False,
                    "detail": "Ollama running but no models installed. Run: ollama pull qwen3:8b",
                    "degraded": True}
        return {"name": "ollama", "ok": True,
                "detail": f"{len(models)} model(s) available: {', '.join(models[:4])}",
                "degraded": False}
    except Exception as e:
        msg = str(e)
        if "connect" in msg.lower() or "refused" in msg.lower():
            detail = "Ollama not running. Start with: ollama serve"
        else:
            detail = f"Ollama error: {msg[:120]}"
        return {"name": "ollama", "ok": False, "detail": detail, "degraded": True}


def _check_stt() -> ComponentStatus:
    try:
        from local_runtime import local_stt
        s = local_stt.status()
        engine = s.get("active_engine", "unknown")
        if engine == "unavailable":
            reason = s.get("import_error") or "No STT backend available"
            return {"name": "stt", "ok": False,
                    "detail": f"STT unavailable: {reason[:100]}", "degraded": True}
        model = s.get("model", "unknown")
        return {"name": "stt", "ok": True,
                "detail": f"STT active: {engine} / {model}", "degraded": False}
    except Exception as e:
        return {"name": "stt", "ok": False,
                "detail": f"STT check error: {e}", "degraded": True}


def _check_tts() -> ComponentStatus:
    try:
        from local_runtime import local_tts
        s = local_tts.status()
        if not s.get("ready"):
            return {"name": "tts", "ok": False,
                    "detail": "Local TTS (say) not ready", "degraded": True}
        return {"name": "tts", "ok": True,
                "detail": "TTS ready (Kokoro → say fallback)", "degraded": False}
    except Exception as e:
        return {"name": "tts", "ok": False,
                "detail": f"TTS check error: {e}", "degraded": True}


def _check_google() -> ComponentStatus:
    try:
        import google_services as gs
        # Lightweight probe: just call the calendar list (1 item max)
        gs._calendar().calendarList().list(maxResults=1).execute()
        return {"name": "google", "ok": True,
                "detail": "Google Calendar + Gmail auth valid", "degraded": False}
    except Exception as e:
        msg = str(e)
        if "credentials" in msg.lower() or "auth" in msg.lower() or "token" in msg.lower():
            detail = "Google auth expired — re-run google_services auth flow"
        else:
            detail = f"Google services error: {msg[:100]}"
        return {"name": "google", "ok": False, "detail": detail, "degraded": True}


def _check_mem0() -> ComponentStatus:
    try:
        import mem0_layer as _m0
        s = _m0.status()
        if s["available"]:
            count = s.get("count", "?")
            return {"name": "mem0", "ok": True,
                    "detail": f"Episodic memory active — {count} memories", "degraded": False}
        return {"name": "mem0", "ok": False,
                "detail": "mem0 unavailable — Ollama not running or nomic-embed-text not pulled",
                "degraded": True}
    except Exception as e:
        return {"name": "mem0", "ok": False,
                "detail": f"mem0 check error: {e}", "degraded": True}


def _check_vault() -> ComponentStatus:
    try:
        import vault
        idx = vault.load_index()
        doc_count = len(idx.get("docs", {}))
        if doc_count == 0:
            return {"name": "vault", "ok": False,
                    "detail": "Vault index empty — run 'refresh the vault'", "degraded": True}
        return {"name": "vault", "ok": True,
                "detail": f"Vault index: {doc_count} documents", "degraded": False}
    except Exception as e:
        return {"name": "vault", "ok": False,
                "detail": f"Vault check error: {e}", "degraded": True}


def _check_watcher() -> ComponentStatus:
    try:
        import jarvis_watcher as _jw
        s = _jw.status()
        if not s["enabled"]:
            return {"name": "watcher", "ok": True,
                    "detail": "Proactive watcher disabled (JARVIS_WATCHER_ENABLED=0)",
                    "degraded": False}
        if s["running"]:
            brief_date = s.get("morning_brief_sent") or "not yet today"
            return {"name": "watcher", "ok": True,
                    "detail": f"Watcher active — morning brief: {brief_date}", "degraded": False}
        return {"name": "watcher", "ok": False,
                "detail": "Watcher thread not running — restart Jarvis", "degraded": True}
    except Exception as e:
        return {"name": "watcher", "ok": False,
                "detail": f"Watcher check error: {e}", "degraded": True}


# ── Check registry ────────────────────────────────────────────────────────────

_CHECKERS = {
    "ollama":  _check_ollama,
    "stt":     _check_stt,
    "tts":     _check_tts,
    "google":  _check_google,
    "mem0":    _check_mem0,
    "vault":   _check_vault,
    "watcher": _check_watcher,
}

# ── Caching (avoid hammering services on every verbal query) ──────────────────

_cache_lock = threading.Lock()
_cache: dict[str, ComponentStatus] = {}
_cache_at: float = 0.0
_CACHE_TTL = 60.0   # re-check every 60 seconds max


def check_all(force: bool = False) -> dict[str, ComponentStatus]:
    """Run all component checks. Results cached for 60 seconds."""
    global _cache, _cache_at
    now = time.monotonic()
    if not force and _cache and (now - _cache_at) < _CACHE_TTL:
        return dict(_cache)
    with _cache_lock:
        now = time.monotonic()
        if not force and _cache and (now - _cache_at) < _CACHE_TTL:
            return dict(_cache)
        results: dict[str, ComponentStatus] = {}
        # Run checkers concurrently
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=len(_CHECKERS)) as pool:
            futures = {pool.submit(fn): name for name, fn in _CHECKERS.items()}
            for future in as_completed(futures, timeout=10.0):
                name = futures[future]
                try:
                    results[name] = future.result(timeout=5.0)
                except Exception as e:
                    results[name] = {"name": name, "ok": False,
                                     "detail": f"Check timed out: {e}", "degraded": True}
        _cache = results
        _cache_at = time.monotonic()
    return dict(_cache)


def degraded() -> list[str]:
    """Return names of degraded components."""
    return [name for name, s in check_all().items() if s.get("degraded")]


def health_summary(force: bool = False) -> str:
    """Return a concise human-readable health report."""
    statuses = check_all(force=force)
    lines: list[str] = []
    ok_names: list[str] = []
    bad: list[str] = []

    for name, s in sorted(statuses.items()):
        icon = "✓" if s["ok"] else "✗"
        if s["ok"]:
            ok_names.append(name)
        else:
            bad.append(f"  {icon} {name}: {s['detail']}")

    if ok_names:
        lines.append(f"  ✓ OK: {', '.join(ok_names)}")
    lines.extend(bad)
    return "\n".join(lines)


def spoken_summary(force: bool = False) -> str:
    """Compact spoken-word health report suitable for TTS."""
    statuses = check_all(force=force)
    bad = [(n, s) for n, s in statuses.items() if s.get("degraded")]
    total = len(statuses)
    ok_count = total - len(bad)

    if not bad:
        return f"All {total} systems are operational."

    if len(bad) == 1:
        n, s = bad[0]
        return f"{ok_count} of {total} systems OK. {n.title()} is degraded: {s['detail']}"

    names = ", ".join(n for n, _ in bad)
    return (
        f"{ok_count} of {total} systems OK. "
        f"{len(bad)} components degraded: {names}. "
        "Say 'health check' for details."
    )
