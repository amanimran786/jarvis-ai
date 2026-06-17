"""skill_monitor.py — Unified skill usage tracking and integration health for Jarvis.

Two responsibilities:
  1. Usage tracking — record every skill/tool invocation with timing and outcome.
     Callers call record_call(); stats are queryable via get_stats().
  2. Integration inventory — check availability of all skills and 3rd-party
     integrations beyond what jarvis_health already covers.

Public API
──────────
  record_call(skill, success, latency_ms, error=None) — record an invocation
  get_stats()     -> dict   per-skill counters + timing
  get_report()    -> dict   health + stats + integration inventory (for /api/monitor)
  integration_status() -> dict  lightweight availability check for all integrations
"""

from __future__ import annotations

import shutil
import threading
import time
from collections import defaultdict, deque
from typing import TypedDict


# ── Usage tracking ────────────────────────────────────────────────────────────

class _SkillStats:
    __slots__ = ("total", "success", "failure", "total_latency_ms", "last_called_at", "last_error")

    def __init__(self) -> None:
        self.total = 0
        self.success = 0
        self.failure = 0
        self.total_latency_ms: float = 0.0
        self.last_called_at: float = 0.0
        self.last_error: str = ""

    def to_dict(self) -> dict:
        avg = (self.total_latency_ms / self.total) if self.total else 0.0
        return {
            "total_calls": self.total,
            "success": self.success,
            "failure": self.failure,
            "avg_latency_ms": round(avg, 1),
            "last_called_at": self.last_called_at,
            "last_error": self.last_error,
        }


_stats_lock = threading.Lock()
_stats: dict[str, _SkillStats] = defaultdict(_SkillStats)

# Rolling 200-entry invocation log for recent activity feed
_recent_log: deque[dict] = deque(maxlen=200)


def record_call(
    skill: str,
    success: bool,
    latency_ms: float,
    error: str | None = None,
) -> None:
    """Record a skill invocation. Thread-safe. No-op on error."""
    if not skill:
        return
    now = time.time()
    with _stats_lock:
        s = _stats[skill]
        s.total += 1
        s.total_latency_ms += max(0.0, float(latency_ms))
        s.last_called_at = now
        if success:
            s.success += 1
        else:
            s.failure += 1
            s.last_error = (error or "")[:200]
        _recent_log.append({
            "skill": skill,
            "ok": success,
            "latency_ms": round(latency_ms, 1),
            "ts": now,
            "error": (error or ""),
        })


def get_stats() -> dict[str, dict]:
    """Return per-skill usage statistics snapshot."""
    with _stats_lock:
        return {skill: s.to_dict() for skill, s in sorted(_stats.items())}


def get_recent_log(n: int = 20) -> list[dict]:
    """Return the last n invocations."""
    with _stats_lock:
        entries = list(_recent_log)
    return entries[-n:]


# ── Integration inventory ─────────────────────────────────────────────────────

class IntegrationStatus(TypedDict):
    name: str
    category: str
    available: bool
    detail: str


def _check_claude() -> IntegrationStatus:
    try:
        from brains import brain_claude
        ok = brain_claude.client is not None
        return {
            "name": "claude",
            "category": "cloud_llm",
            "available": ok,
            "detail": "Anthropic API key configured" if ok else "ANTHROPIC_API_KEY not set",
        }
    except Exception as e:
        return {"name": "claude", "category": "cloud_llm", "available": False,
                "detail": f"Import error: {e}"}


def _check_gemini() -> IntegrationStatus:
    try:
        from brains import brain_gemini
        ok = brain_gemini.client is not None
        return {
            "name": "gemini",
            "category": "cloud_llm",
            "available": ok,
            "detail": "Gemini API key configured" if ok else "GEMINI_API_KEY not set or google-genai not installed",
        }
    except Exception as e:
        return {"name": "gemini", "category": "cloud_llm", "available": False,
                "detail": f"Import error: {e}"}


def _check_kimi() -> IntegrationStatus:
    try:
        from brains import brain_kimi
        key = getattr(brain_kimi, "_KIMI_API_KEY", None) or ""
        ok = bool(key)
        return {
            "name": "kimi",
            "category": "cloud_llm",
            "available": ok,
            "detail": "Kimi (Moonshot) API key configured" if ok else "MOONSHOT_API_KEY not set",
        }
    except Exception as e:
        return {"name": "kimi", "category": "cloud_llm", "available": False,
                "detail": f"Import error: {e}"}


def _check_osint_tools() -> list[IntegrationStatus]:
    results = []
    tools = [
        ("maigret",   "osint", "Username footprint scanner"),
        ("dnstwist",  "osint", "Domain typo-squatting scanner"),
        ("subfinder", "osint", "Passive subdomain enumerator"),
        ("whois",     "osint", "WHOIS lookup"),
    ]
    for name, cat, desc in tools:
        cmd = shutil.which(name)
        results.append({
            "name": name,
            "category": cat,
            "available": bool(cmd),
            "detail": f"{desc}: {cmd}" if cmd else f"{desc}: not found in PATH (pip install {name})",
        })
    return results


def _check_elevenlabs() -> IntegrationStatus:
    import os
    key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    return {
        "name": "elevenlabs",
        "category": "tts",
        "available": bool(key),
        "detail": "ElevenLabs TTS key configured" if key else "ELEVENLABS_API_KEY not set (optional)",
    }


def _check_browser() -> IntegrationStatus:
    try:
        import browser as _b
        ok = True
        return {"name": "browser", "category": "system", "available": ok,
                "detail": "Browser automation available"}
    except Exception as e:
        return {"name": "browser", "category": "system", "available": False,
                "detail": f"Browser module error: {e}"}


def _check_meeting() -> IntegrationStatus:
    try:
        import meeting_listener as _ml
        return {"name": "meeting_listener", "category": "system", "available": True,
                "detail": "Smart Listen available"}
    except Exception as e:
        return {"name": "meeting_listener", "category": "system", "available": False,
                "detail": f"Meeting listener error: {e}"}


def _check_camera() -> IntegrationStatus:
    try:
        import camera as _cam
        return {"name": "camera", "category": "system", "available": True,
                "detail": "Webcam module available"}
    except Exception as e:
        return {"name": "camera", "category": "system", "available": False,
                "detail": f"Camera module error: {e}"}


def _check_hardware() -> IntegrationStatus:
    try:
        import hardware as _hw
        return {"name": "hardware", "category": "system", "available": True,
                "detail": "Hardware control available"}
    except Exception as e:
        return {"name": "hardware", "category": "system", "available": False,
                "detail": f"Hardware module error: {e}"}


_INT_CACHE_LOCK = threading.Lock()
_int_cache: dict[str, IntegrationStatus] = {}
_int_cache_at: float = 0.0
_INT_CACHE_TTL = 120.0


def integration_status(force: bool = False) -> dict[str, IntegrationStatus]:
    """Return availability of all integrations. Cached 120 s."""
    global _int_cache, _int_cache_at
    now = time.monotonic()
    if not force and _int_cache and (now - _int_cache_at) < _INT_CACHE_TTL:
        return dict(_int_cache)
    with _INT_CACHE_LOCK:
        now = time.monotonic()
        if not force and _int_cache and (now - _int_cache_at) < _INT_CACHE_TTL:
            return dict(_int_cache)

        out: dict[str, IntegrationStatus] = {}
        single_checks = [
            _check_claude,
            _check_gemini,
            _check_kimi,
            _check_elevenlabs,
            _check_browser,
            _check_meeting,
            _check_camera,
            _check_hardware,
        ]
        from concurrent.futures import ThreadPoolExecutor, as_completed
        pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="int_check")
        futures = {pool.submit(fn): fn.__name__ for fn in single_checks}
        try:
            for future in as_completed(futures, timeout=8.0):
                try:
                    result = future.result(timeout=0)
                    out[result["name"]] = result
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        for item in _check_osint_tools():
            out[item["name"]] = item

        _int_cache = out
        _int_cache_at = time.monotonic()
    return dict(_int_cache)


# ── Full report ───────────────────────────────────────────────────────────────

def get_report(force: bool = False) -> dict:
    """Combine health, usage stats, integrations, and agent roster."""
    import jarvis_health as _jh
    from tool_registry import TOOLS, TOOL_SPECS
    from config import AGENT_ROSTER

    health = _jh.check_all(force=force)
    integrations = integration_status(force=force)
    stats = get_stats()

    # Skill catalog: merge tool_registry TOOLS with any usage stats
    skills = {}
    for name, description in TOOLS.items():
        spec = TOOL_SPECS.get(name)
        skills[name] = {
            "description": description,
            "side_effects": spec.side_effects if spec else None,
            "timeout_seconds": spec.timeout_seconds if spec else None,
            "usage": stats.get(name, {}),
        }

    # Agent roster: name + role + usage stats
    agents = {}
    for agent_id, agent_def in AGENT_ROSTER.items():
        agents[agent_id] = {
            "role": agent_def.get("role", ""),
            "usage": stats.get(agent_id, {}),
        }

    total_calls = sum(s.get("total_calls", 0) for s in stats.values())
    total_errors = sum(s.get("failure", 0) for s in stats.values())

    return {
        "health": health,
        "integrations": integrations,
        "skills": skills,
        "agents": agents,
        "summary": {
            "total_skill_calls": total_calls,
            "total_errors": total_errors,
            "health_ok": [n for n, s in health.items() if s.get("ok")],
            "health_degraded": [n for n, s in health.items() if s.get("degraded")],
            "integrations_available": [n for n, i in integrations.items() if i.get("available")],
            "integrations_unavailable": [n for n, i in integrations.items() if not i.get("available")],
        },
        "recent_log": get_recent_log(20),
    }
