"""
harness/budget.py — Three-tier provider rate limiter and budget dashboard.

LOCAL_FIRST priority order:
  1. ollama        (local)       — free, unlimited, always first
  2. ollama_cloud  (remote free) — api.ollama.com, session + weekly caps
  3. anthropic / openai / gemini (paid) — 80k soft / 100k hard tokens/hr

Every cloud call in model_router._candidate_stream() checks this before
executing. Soft limit logs a warning and biases routing toward lower tiers.
Hard limit raises RuntimeError so _execute_plan_stream falls through to local.

Budget log: logs/budget.jsonl (separate from usage_log.jsonl)
Fields per entry: ts, provider, model, tokens_in, tokens_out, session_id, running_total_in
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

def _base_dir() -> Path:
    try:
        import sys
        if getattr(sys, "frozen", False):
            import runtime_state
            return runtime_state.app_data_dir()
    except Exception:
        pass
    return Path(__file__).resolve().parent.parent


def _budget_log_path() -> Path:
    p = _base_dir() / "logs" / "budget.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ── Provider thresholds ────────────────────────────────────────────────────────

# Paid cloud: hourly token limits
_HOURLY_SOFT: dict[str, int] = {
    "anthropic": int(os.getenv("JARVIS_ANTHROPIC_SOFT_TOKENS_HR", "80000")),
    "openai":    int(os.getenv("JARVIS_OPENAI_SOFT_TOKENS_HR",    "80000")),
    "gemini":    int(os.getenv("JARVIS_GEMINI_SOFT_TOKENS_HR",    "80000")),
}
_HOURLY_HARD: dict[str, int] = {
    "anthropic": int(os.getenv("JARVIS_ANTHROPIC_HARD_TOKENS_HR", "100000")),
    "openai":    int(os.getenv("JARVIS_OPENAI_HARD_TOKENS_HR",    "100000")),
    "gemini":    int(os.getenv("JARVIS_GEMINI_HARD_TOKENS_HR",    "100000")),
}

# Ollama Cloud free tier: session (60-min rolling) and weekly caps
OLLAMA_CLOUD_SESSION_SOFT = int(os.getenv("JARVIS_OLLAMA_CLOUD_SESSION_SOFT", "160000"))  # 80% of 200k
OLLAMA_CLOUD_SESSION_HARD = int(os.getenv("JARVIS_OLLAMA_CLOUD_SESSION_HARD", "200000"))
OLLAMA_CLOUD_WEEKLY_SOFT  = int(os.getenv("JARVIS_OLLAMA_CLOUD_WEEKLY_SOFT",  "800000"))  # 80% of 1M
OLLAMA_CLOUD_WEEKLY_HARD  = int(os.getenv("JARVIS_OLLAMA_CLOUD_WEEKLY_HARD",  "1000000"))

# ── Session running totals (in-memory, reset on process start) ─────────────────

_lock = threading.Lock()
_SESSION_ID = str(uuid.uuid4())

# Running totals per provider for this process session
_session_tokens_in: dict[str, int] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


# ── Core usage query ──────────────────────────────────────────────────────────

def _tokens_in_last_hours(provider: str, hours: int) -> int:
    """Read from usage_log.jsonl via usage_tracker (shared source of truth)."""
    try:
        import usage_tracker
        return sum(
            int(r.get("prompt_tokens") or 0)
            for r in usage_tracker.entries(hours=hours)
            if (r.get("provider") or "").lower() == provider.lower()
            and not r.get("local")
        )
    except Exception:
        return 0


def _ollama_cloud_tokens_in_window(hours: int) -> int:
    try:
        import usage_tracker
        return sum(
            int(r.get("prompt_tokens") or 0)
            for r in usage_tracker.entries(hours=hours)
            if (r.get("provider") or "").lower() == "ollama_cloud"
        )
    except Exception:
        return 0


# ── Check / enforce ───────────────────────────────────────────────────────────

def check(provider: str) -> dict:
    """
    Return budget state for a provider before executing a call.

    Returns dict with:
      soft      — bool: soft limit exceeded (warn, prefer local)
      hard      — bool: hard limit exceeded (block, raise in _candidate_stream)
      used_1h   — int: tokens in last hour (paid providers)
      used_session — int: tokens in last 60 min (ollama_cloud)
      used_week    — int: tokens in last 7 days (ollama_cloud)
      limit_soft   — int: soft threshold
      limit_hard   — int: hard threshold
      provider     — str
    """
    lower = (provider or "").lower()

    if lower == "ollama":
        # Local Ollama: always OK
        return {
            "soft": False, "hard": False,
            "used_1h": 0, "used_session": 0, "used_week": 0,
            "limit_soft": 0, "limit_hard": 0,
            "provider": lower, "tier": "local",
        }

    if lower == "ollama_cloud":
        used_session = _ollama_cloud_tokens_in_window(hours=1)
        used_week = _ollama_cloud_tokens_in_window(hours=7 * 24)
        hard = (used_session >= OLLAMA_CLOUD_SESSION_HARD) or (used_week >= OLLAMA_CLOUD_WEEKLY_HARD)
        soft = (not hard) and (
            (used_session >= OLLAMA_CLOUD_SESSION_SOFT) or (used_week >= OLLAMA_CLOUD_WEEKLY_SOFT)
        )
        if hard:
            log.warning("[Budget] ollama_cloud hard limit reached (session=%d, week=%d)", used_session, used_week)
        elif soft:
            log.warning("[Budget] ollama_cloud soft limit hit (session=%d, week=%d) — prefer local", used_session, used_week)
        return {
            "soft": soft, "hard": hard,
            "used_1h": used_session,
            "used_session": used_session,
            "used_week": used_week,
            "limit_soft": OLLAMA_CLOUD_SESSION_SOFT,
            "limit_hard": OLLAMA_CLOUD_SESSION_HARD,
            "provider": lower, "tier": "cloud_free",
        }

    # Paid providers: anthropic, openai, gemini
    if lower in _HOURLY_HARD:
        used_1h = _tokens_in_last_hours(lower, hours=1)
        hard = used_1h >= _HOURLY_HARD[lower]
        soft = (not hard) and (used_1h >= _HOURLY_SOFT[lower])
        if hard:
            log.warning(
                "[Budget] %s hard limit reached (%d/%d tokens/hr) — blocking cloud call",
                lower, used_1h, _HOURLY_HARD[lower],
            )
        elif soft:
            log.warning(
                "[Budget] %s soft limit hit (%d/%d tokens/hr) — routing local preferred",
                lower, used_1h, _HOURLY_SOFT[lower],
            )
        return {
            "soft": soft, "hard": hard,
            "used_1h": used_1h, "used_session": 0, "used_week": 0,
            "limit_soft": _HOURLY_SOFT[lower],
            "limit_hard": _HOURLY_HARD[lower],
            "provider": lower, "tier": "paid",
        }

    # Unknown provider — fail open
    return {
        "soft": False, "hard": False,
        "used_1h": 0, "used_session": 0, "used_week": 0,
        "limit_soft": 0, "limit_hard": 0,
        "provider": lower, "tier": "unknown",
    }


# ── Budget JSONL ledger ───────────────────────────────────────────────────────

def record(
    *,
    provider: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    session_id: str | None = None,
) -> None:
    """Append one entry to logs/budget.jsonl with running_total_in."""
    lower = (provider or "").lower()
    sid = session_id or _SESSION_ID

    with _lock:
        prev = _session_tokens_in.get(lower, 0)
        running = prev + tokens_in
        _session_tokens_in[lower] = running

    entry = {
        "ts": _now_iso(),
        "provider": lower,
        "model": model or "",
        "tokens_in": int(tokens_in),
        "tokens_out": int(tokens_out),
        "session_id": sid,
        "running_total_in": running,
    }

    path = _budget_log_path()
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        log.debug("[Budget] Could not write budget.jsonl: %s", path)


# ── Context pressure detection ────────────────────────────────────────────────

def context_pressure(context_used_tokens: int, context_budget_tokens: int) -> str:
    """
    Returns one of: "none", "compress", "switch"

    "compress" (>75%): caller should drop episodic memory blocks and retry compile.
    "switch"   (>90%): caller should route to a larger-context local model.
    """
    if context_budget_tokens <= 0:
        return "none"
    ratio = context_used_tokens / context_budget_tokens
    if ratio >= 0.90:
        return "switch"
    if ratio >= 0.75:
        return "compress"
    return "none"


# ── /budget status text ───────────────────────────────────────────────────────

def status_text() -> str:
    """
    Formatted table for the /budget command.

    Provider        Used (1hr)   Used (session)  Used (week)   Limit          Status
    ollama_local    —            —               —             ∞              LOCAL_FIRST: ON
    ollama_cloud    0            0               0             200k/1M free   OK
    Anthropic       0            —               —             100k/hr        OK
    OpenAI          0            —               —             100k/hr        OK
    Gemini          0            —               —             100k/hr        OK
    """
    try:
        from config import LOCAL_STRICT_FIRST
    except Exception:
        LOCAL_STRICT_FIRST = True

    rows: list[str] = []
    rows.append(
        f"{'Provider':<16} {'1-hr tokens':<14} {'Session':>9} {'Week':>10}  {'Limit':<18} Status"
    )
    rows.append("-" * 80)

    # Local Ollama
    lf_label = "LOCAL_FIRST: ON" if LOCAL_STRICT_FIRST else "LOCAL_FIRST: off"
    rows.append(f"{'ollama (local)':<16} {'—':<14} {'—':>9} {'—':>10}  {'unlimited':<18} {lf_label}")

    # Ollama Cloud
    oc = check("ollama_cloud")
    oc_status = "HARD LIMIT" if oc["hard"] else ("soft limit" if oc["soft"] else "OK")
    oc_session_pct = f"{oc['used_session']/OLLAMA_CLOUD_SESSION_HARD*100:.0f}%" if OLLAMA_CLOUD_SESSION_HARD else "—"
    oc_week_pct    = f"{oc['used_week']/OLLAMA_CLOUD_WEEKLY_HARD*100:.0f}%" if OLLAMA_CLOUD_WEEKLY_HARD else "—"
    rows.append(
        f"{'ollama_cloud':<16} {oc['used_session']:<14,} {oc_session_pct:>9} {oc_week_pct:>10}  {'200k/1M free':<18} {oc_status}"
    )

    # Paid providers
    for provider in ("anthropic", "openai", "gemini"):
        r = check(provider)
        status = "HARD LIMIT" if r["hard"] else ("soft limit" if r["soft"] else "OK")
        limit_str = f"{r['limit_hard']//1000}k/hr"
        rows.append(
            f"{provider.capitalize():<16} {r['used_1h']:<14,} {'—':>9} {'—':>10}  {limit_str:<18} {status}"
        )

    rows.append("")

    # Context pressure hint
    rows.append("Context pressure check — call context_pressure(used, budget) before each query.")

    # Session running totals from in-memory tracker
    with _lock:
        totals = dict(_session_tokens_in)
    if totals:
        rows.append("\nThis-session running totals (tokens_in):")
        for p, t in sorted(totals.items()):
            rows.append(f"  {p}: {t:,}")

    return "\n".join(rows)
