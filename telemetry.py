from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ROUTING_LOG = ROOT / "routing_log.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_route_decision(*, user_input: str, mode: str, tier: str, plan: dict, selected: dict | None, reason: str) -> None:
    entry = {
        "timestamp": _now_iso(),
        "mode": mode,
        "tier": tier,
        "reason": reason,
        "selected": selected or {},
        "plan": plan,
        "input_preview": (user_input or "")[:200],
    }
    with ROUTING_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def recent_routes(limit: int = 10) -> list[dict]:
    if not ROUTING_LOG.exists():
        return []
    rows: list[dict] = []
    with ROUTING_LOG.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-limit:]
