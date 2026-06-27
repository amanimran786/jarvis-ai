"""
harness/adaptive_router.py — Route quality tracking and adaptive demotion.

Reads logs/self_eval.jsonl to compute per-route quality scores. Routes that
consistently score below DEMOTION_THRESHOLD with enough samples are flagged as
"demoted". The api.py route_stream wrapper checks this before returning a response:
demoted text-only routes are replaced with a direct LLM fallback.

Side-effect routes (Calendar, Gmail, Messages, Browser) are NEVER demoted —
they are logged and tracked but not intercepted.

Public API:
    notify_route_used(query, route) -> None
    record_quality(route, quality) -> None
    get_demoted_routes() -> frozenset[str]
    is_demoted(route) -> bool
    is_side_effect_route(route) -> bool
    refresh() -> None
    route_quality_report(n=200) -> str    — for diagnostics
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

DEMOTION_THRESHOLD: float = 0.65      # routes below this are demoted
DEMOTION_MIN_SAMPLES: int = 5         # need at least this many scored calls
REFRESH_INTERVAL_SECS: float = 300.0  # re-read self_eval.jsonl every 5 min

# Routes where interception is UNSAFE (side effects: API calls, emails, events).
# These are tracked but never actually bypassed.
SIDE_EFFECT_ROUTES: frozenset[str] = frozenset({
    "Calendar", "Gmail", "Messages", "Browser", "Meeting",
    "Contacts", "Clipboard", "App", "Hardware", "Screen",
})

# Routes where LLM fallback is safe (pure text responses).
SAFE_TO_DEMOTE: frozenset[str] = frozenset({
    "Status", "Knowledge", "Vault", "Search", "Notes",
    "Self-Eval", "Self-Review", "Self-Improve", "Jarvis",
    "Interview", "Sonnet", "Git", "Skill", "Memory",
    "System", "Budget", "Vision", "Weather",
})


# ── Path helpers ───────────────────────────────────────────────────────────────

def _base_dir() -> Path:
    try:
        from harness.audit import _base_dir as _ab
        return _ab()
    except Exception:
        return Path(__file__).resolve().parent.parent


def _self_eval_log_path() -> Path:
    return _base_dir() / "logs" / "self_eval.jsonl"


def _quality_store_path() -> Path:
    return _base_dir() / "logs" / "route_quality.json"


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class RouteStats:
    route: str
    count: int = 0
    quality_sum: float = 0.0
    routing_sum: float = 0.0
    relevance_sum: float = 0.0
    recent: deque = field(default_factory=lambda: deque(maxlen=20))

    @property
    def avg_quality(self) -> float | None:
        return round(self.quality_sum / self.count, 3) if self.count else None

    @property
    def avg_routing(self) -> float | None:
        return round(self.routing_sum / self.count, 3) if self.count else None

    @property
    def avg_relevance(self) -> float | None:
        return round(self.relevance_sum / self.count, 3) if self.count else None

    def is_demoted(self, threshold: float = DEMOTION_THRESHOLD,
                   min_samples: int = DEMOTION_MIN_SAMPLES) -> bool:
        if self.count < min_samples:
            return False
        q = self.avg_quality
        return q is not None and q < threshold

    def record(self, quality: float, routing: float = 0.5, relevance: float = 0.5) -> None:
        self.count += 1
        self.quality_sum += quality
        self.routing_sum += routing
        self.relevance_sum += relevance
        self.recent.append(quality)

    def to_dict(self) -> dict:
        return {
            "route": self.route,
            "count": self.count,
            "avg_quality": self.avg_quality,
            "avg_routing": self.avg_routing,
            "avg_relevance": self.avg_relevance,
            "demoted": self.is_demoted(),
            "recent": list(self.recent),
        }


# ── Module-level state ────────────────────────────────────────────────────────

_lock = threading.Lock()
_stats: dict[str, RouteStats] = {}
_demoted: frozenset[str] = frozenset()
_last_refresh: float = 0.0
_pending_outcomes: list[tuple[str, float, float, float]] = []  # (route, q, r, rel)


# ── Persistence ────────────────────────────────────────────────────────────────

def _load_persisted() -> dict[str, RouteStats]:
    path = _quality_store_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        result: dict[str, RouteStats] = {}
        for entry in data:
            rs = RouteStats(route=entry["route"])
            rs.count = entry.get("count", 0)
            rs.quality_sum = entry.get("avg_quality", 0.0) * rs.count
            rs.routing_sum = entry.get("avg_routing", 0.5) * rs.count
            rs.relevance_sum = entry.get("avg_relevance", 0.5) * rs.count
            rs.recent = deque(entry.get("recent", []), maxlen=20)
            result[entry["route"]] = rs
        return result
    except Exception as exc:
        log.debug("[adaptive_router] load_persisted failed: %s", exc)
        return {}


def _save_persisted(stats: dict[str, RouteStats]) -> None:
    path = _quality_store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        data = [s.to_dict() for s in sorted(stats.values(), key=lambda s: -(s.count or 0))]
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        log.debug("[adaptive_router] save_persisted failed: %s", exc)


# ── Self-eval log reader ───────────────────────────────────────────────────────

def _load_from_self_eval(n: int = 200) -> dict[str, RouteStats]:
    """Read the last n entries from self_eval.jsonl, return per-route stats."""
    from harness.self_eval_log import load_recent
    records = load_recent(n)
    result: dict[str, RouteStats] = defaultdict(lambda: RouteStats(route=""))
    for rec in records:
        route = (rec.get("route") or "").strip()
        if not route:
            continue
        if route not in result:
            result[route] = RouteStats(route=route)
        rs = result[route]
        rs.record(
            quality=rec.get("response_quality", 0.5),
            routing=rec.get("routing_accuracy", 0.5),
            relevance=rec.get("response_relevance", 0.5),
        )
    return dict(result)


# ── Refresh ────────────────────────────────────────────────────────────────────

def _merge(base: dict[str, RouteStats], overlay: dict[str, RouteStats]) -> dict[str, RouteStats]:
    """Merge overlay into base, summing counts and quality."""
    merged = {k: v for k, v in base.items()}
    for route, rs in overlay.items():
        if route in merged:
            existing = merged[route]
            existing.count += rs.count
            existing.quality_sum += rs.quality_sum
            existing.routing_sum += rs.routing_sum
            existing.relevance_sum += rs.relevance_sum
            existing.recent.extend(rs.recent)
        else:
            merged[route] = rs
    return merged


def refresh() -> None:
    """Reload route quality from self_eval.jsonl + persisted store. Thread-safe."""
    global _stats, _demoted, _last_refresh
    try:
        persisted = _load_persisted()
        from_log = _load_from_self_eval(n=200)
        merged = _merge(persisted, from_log)

        # Apply pending in-session outcomes
        with _lock:
            for (route, q, r, rel) in list(_pending_outcomes):
                if route not in merged:
                    merged[route] = RouteStats(route=route)
                merged[route].record(q, r, rel)
            _pending_outcomes.clear()

        new_demoted = frozenset(
            route for route, rs in merged.items()
            if rs.is_demoted()
        )

        with _lock:
            _stats = merged
            _demoted = new_demoted
            _last_refresh = time.monotonic()

        # Persist updated scores
        _save_persisted(merged)

        if new_demoted:
            log.info("[adaptive_router] Demoted routes: %s", sorted(new_demoted))
    except Exception as exc:
        log.warning("[adaptive_router] refresh failed: %s", exc)


def _maybe_refresh() -> None:
    """Refresh if stale (called on each hot path, cheap no-op if fresh)."""
    global _last_refresh
    age = time.monotonic() - _last_refresh
    if age > REFRESH_INTERVAL_SECS:
        threading.Thread(target=refresh, daemon=True).start()


# ── Public API ─────────────────────────────────────────────────────────────────

def notify_route_used(query: str, route: str) -> None:
    """Record that a route was used for a query (quality unknown yet). No-op if route blank."""
    _maybe_refresh()


def record_quality(route: str, quality: float, routing: float = 0.5,
                   relevance: float = 0.5) -> None:
    """Record a quality score for a route. Called after self-eval completes."""
    if not route:
        return
    with _lock:
        if route not in _stats:
            _stats[route] = RouteStats(route=route)
        _stats[route].record(quality, routing, relevance)
        _pending_outcomes.append((route, quality, routing, relevance))
    _maybe_refresh()


def get_demoted_routes() -> frozenset[str]:
    """Return the current set of demoted routes."""
    _maybe_refresh()
    with _lock:
        return _demoted


def is_demoted(route: str) -> bool:
    """True if this route is currently demoted (below threshold with enough samples)."""
    if not route:
        return False
    with _lock:
        rs = _stats.get(route)
    if rs is None:
        return False
    return rs.is_demoted()


def is_side_effect_route(route: str) -> bool:
    """True if intercepting this route is unsafe (side effects: API, emails, events)."""
    return route in SIDE_EFFECT_ROUTES


def should_fallback(route: str) -> bool:
    """True if this route should be replaced with an LLM fallback when demoted."""
    return is_demoted(route) and route in SAFE_TO_DEMOTE and route not in SIDE_EFFECT_ROUTES


def get_stats() -> dict[str, dict]:
    """Return per-route stats dict (for diagnostics)."""
    with _lock:
        return {k: v.to_dict() for k, v in _stats.items()}


def route_quality_report(n: int = 200) -> str:
    """Short text report of route quality for /diagnose or logging."""
    refresh()
    with _lock:
        stats_snapshot = dict(_stats)
        demoted_snapshot = frozenset(_demoted)

    if not stats_snapshot:
        return "No route quality data yet — need more scored interactions."

    lines = [f"Route quality ({n} responses analyzed):"]
    for route, rs in sorted(stats_snapshot.items(), key=lambda kv: -(kv[1].count)):
        q = f"{rs.avg_quality:.2f}" if rs.avg_quality is not None else "—"
        r = f"{rs.avg_routing:.2f}" if rs.avg_routing is not None else "—"
        flag = " ⚠ DEMOTED" if route in demoted_snapshot else ""
        safe_flag = " [side-effect]" if route in SIDE_EFFECT_ROUTES else ""
        lines.append(
            f"  {route}: quality={q}  routing={r}  n={rs.count}{flag}{safe_flag}"
        )

    if demoted_snapshot:
        safe_demoted = sorted(demoted_snapshot - SIDE_EFFECT_ROUTES)
        unsafe_demoted = sorted(demoted_snapshot & SIDE_EFFECT_ROUTES)
        if safe_demoted:
            lines.append(f"\nActive fallbacks: {', '.join(safe_demoted)}")
        if unsafe_demoted:
            lines.append(f"Tracked (no bypass): {', '.join(unsafe_demoted)}")
    else:
        lines.append("\nNo routes demoted — quality looks good across all routes.")

    return "\n".join(lines)


# ── Fallback stream builder ────────────────────────────────────────────────────

def build_fallback_stream(query: str, demoted_route: str):
    """Return a generator that calls the LLM directly, bypassing a demoted route.

    Used by api.py when should_fallback(route) is True.
    """
    def _gen():
        try:
            from brains.brain_ollama import ask_local, get_best_available
            model = get_best_available()
            system = (
                f"[Note: Route '{demoted_route}' was bypassed due to low quality scores. "
                f"Respond directly and helpfully.]"
            )
            response = ask_local(query, model=model, system_extra=system)
            yield response or "[No response from fallback model]"
        except Exception as exc:
            log.warning("[adaptive_router] fallback stream failed: %s", exc)
            yield f"[Fallback failed: {exc}]"

    return _gen()


# ── CLI ────────────────────────────────────────────────────────────────────────

def _cli(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="harness.adaptive_router")
    parser.add_argument("--n", type=int, default=200, help="Entries to analyze")
    parser.add_argument("--refresh", action="store_true", help="Force refresh from self_eval log")
    args = parser.parse_args(argv)
    if args.refresh:
        refresh()
    print(route_quality_report(n=args.n))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
