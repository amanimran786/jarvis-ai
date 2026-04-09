"""
Lightweight usage ledger for Jarvis provider calls.

This records actual provider/model usage metadata whenever the backends expose
it, falls back to conservative token estimates when they do not, and keeps a
simple append-only JSONL log for later analysis.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
USAGE_LOG = ROOT / "usage_log.jsonl"
USAGE_STATE = ROOT / "usage_state.json"

_LOCK = threading.Lock()
_MAX_RECENT = 50

# These are blended assumptions per million total tokens, not exact provider
# billing. They are meant for directional cost analysis inside Jarvis.
BLENDED_USD_PER_MILLION_TOTAL = {
    "gpt-4o-mini": 0.15,
    "gpt-4o": 2.50,
    "gemini-2.5-flash": 0.35,
    "gemini-2.5-pro": 3.50,
    "claude-haiku-4-5-20251001": 0.80,
    "claude-sonnet-4-6": 3.00,
    "claude-opus-4-6": 15.00,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _ensure_state() -> dict:
    if not USAGE_STATE.exists():
        return {"seq": 0, "last_updated": None}
    try:
        return json.loads(USAGE_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"seq": 0, "last_updated": None}


def _save_state(state: dict) -> None:
    tmp = USAGE_STATE.with_name(f"{USAGE_STATE.stem}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(USAGE_STATE)


def _estimate_tokens_from_messages(messages: list[dict]) -> int:
    total_chars = 0
    for message in messages or []:
        total_chars += len(message.get("content", "") or "")
        total_chars += len(message.get("role", "") or "")
    return max(1, total_chars // 4) if total_chars else 0


def _estimate_tokens_from_text(text: str) -> int:
    return max(1, len((text or "").strip()) // 4) if (text or "").strip() else 0


def _cost_for_model(model: str, total_tokens: int) -> float | None:
    rate = BLENDED_USD_PER_MILLION_TOTAL.get(model)
    if rate is None:
        return None
    return round((total_tokens / 1_000_000) * rate, 8)


def current_seq() -> int:
    with _LOCK:
        return int(_ensure_state().get("seq", 0))


def record(
    *,
    provider: str,
    model: str,
    local: bool,
    source: str = "",
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    messages: list[dict] | None = None,
    response_text: str = "",
    estimated: bool = False,
    metadata: dict | None = None,
) -> dict:
    with _LOCK:
        state = _ensure_state()
        seq = int(state.get("seq", 0)) + 1
        state["seq"] = seq
        state["last_updated"] = _now_iso()
        _save_state(state)

        if prompt_tokens is None and messages is not None:
            prompt_tokens = _estimate_tokens_from_messages(messages)
            estimated = True
        if completion_tokens is None and response_text:
            completion_tokens = _estimate_tokens_from_text(response_text)
            estimated = True
        if total_tokens is None:
            total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)

        entry = {
            "id": uuid.uuid4().hex[:12],
            "seq": seq,
            "timestamp": _now_iso(),
            "provider": provider,
            "model": model,
            "local": bool(local),
            "source": source,
            "prompt_tokens": int(prompt_tokens or 0),
            "completion_tokens": int(completion_tokens or 0),
            "total_tokens": int(total_tokens or 0),
            "estimated": bool(estimated),
            "estimated_cost_usd": 0.0 if local else _cost_for_model(model, int(total_tokens or 0)),
            "pricing_basis": "blended_total_token_assumption" if not local else "local_zero",
            "metadata": metadata or {},
        }
        with USAGE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry


def _load_entries_unlocked() -> list[dict]:
    if not USAGE_LOG.exists():
        return []
    rows = []
    with USAGE_LOG.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def entries(hours: int = 24, since_seq: int = 0) -> list[dict]:
    cutoff = _now() - timedelta(hours=hours)
    with _LOCK:
        rows = _load_entries_unlocked()
    result = []
    for row in rows:
        if row.get("seq", 0) <= since_seq:
            continue
        ts = _parse_iso(row.get("timestamp", ""))
        if ts and ts >= cutoff:
            result.append(row)
    return result


def summarize(hours: int = 24, since_seq: int = 0, include_recent: int = 10) -> dict:
    rows = entries(hours=hours, since_seq=since_seq)
    summary = {
        "hours": hours,
        "since_seq": since_seq,
        "call_count": len(rows),
        "local_call_count": 0,
        "cloud_call_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": 0.0,
        "estimated_entry_count": 0,
        "by_provider": {},
        "by_model": {},
        "recent": rows[-include_recent:] if include_recent else [],
    }

    provider_buckets: dict[str, dict] = {}
    model_buckets: dict[str, dict] = {}
    for row in rows:
        local = bool(row.get("local"))
        prompt_tokens = int(row.get("prompt_tokens") or 0)
        completion_tokens = int(row.get("completion_tokens") or 0)
        total_tokens = int(row.get("total_tokens") or 0)
        cost = row.get("estimated_cost_usd")

        if local:
            summary["local_call_count"] += 1
        else:
            summary["cloud_call_count"] += 1

        summary["prompt_tokens"] += prompt_tokens
        summary["completion_tokens"] += completion_tokens
        summary["total_tokens"] += total_tokens
        if isinstance(cost, (int, float)):
            summary["estimated_cost_usd"] += float(cost)
        if row.get("estimated"):
            summary["estimated_entry_count"] += 1

        provider = row.get("provider", "unknown")
        model = row.get("model", "unknown")

        provider_bucket = provider_buckets.setdefault(provider, {
            "call_count": 0,
            "local_call_count": 0,
            "cloud_call_count": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
        })
        provider_bucket["call_count"] += 1
        provider_bucket["total_tokens"] += total_tokens
        if local:
            provider_bucket["local_call_count"] += 1
        else:
            provider_bucket["cloud_call_count"] += 1
        if isinstance(cost, (int, float)):
            provider_bucket["estimated_cost_usd"] += float(cost)

        model_bucket = model_buckets.setdefault(model, {
            "provider": provider,
            "local": local,
            "call_count": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
        })
        model_bucket["call_count"] += 1
        model_bucket["prompt_tokens"] += prompt_tokens
        model_bucket["completion_tokens"] += completion_tokens
        model_bucket["total_tokens"] += total_tokens
        if isinstance(cost, (int, float)):
            model_bucket["estimated_cost_usd"] += float(cost)

    summary["estimated_cost_usd"] = round(summary["estimated_cost_usd"], 8)
    summary["by_provider"] = {
        key: {**value, "estimated_cost_usd": round(value["estimated_cost_usd"], 8)}
        for key, value in provider_buckets.items()
    }
    summary["by_model"] = {
        key: {**value, "estimated_cost_usd": round(value["estimated_cost_usd"], 8)}
        for key, value in model_buckets.items()
    }
    return summary


def summary_text(hours: int = 24) -> str:
    data = summarize(hours=hours, include_recent=0)
    if data["call_count"] == 0:
        return "I have no provider usage recorded yet for that window."

    top_models = sorted(
        data["by_model"].items(),
        key=lambda item: item[1]["total_tokens"],
        reverse=True,
    )[:3]
    top_text = ", ".join(
        f"{model} with {stats['total_tokens']} total tokens"
        for model, stats in top_models
    ) or "no model details"

    return (
        f"In the last {hours} hours I recorded {data['call_count']} model calls. "
        f"{data['local_call_count']} were local and {data['cloud_call_count']} were cloud. "
        f"That totaled {data['total_tokens']} tokens across providers, with an estimated cloud cost of "
        f"{data['estimated_cost_usd']:.6f} dollars based on the configured blended token rates. "
        f"The heaviest models were {top_text}."
    )
