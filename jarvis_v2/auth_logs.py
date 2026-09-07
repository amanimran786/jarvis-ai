"""Deterministic triage of normalized local authentication JSONL snapshots."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from collections import deque
from datetime import datetime, timezone

from .tools import LocalToolError

MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_EVENTS = 10_000
MAX_LINE_BYTES = 16_384
MAX_ALERTS = 100
MAX_EVIDENCE_LINES = 20
WINDOW_SECONDS = 300
FAILURE_THRESHOLD = 5


def _text(row: dict, name: str) -> str:
    value = row[name]
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("invalid field")
    return value


def _object(pairs: list[tuple]) -> dict:
    row = {}
    for key, value in pairs:
        if key in row:
            raise ValueError("duplicate JSON key")
        row[key] = value
    return row


def analyze_auth_log(data: bytes) -> dict:
    """Inspect a bounded snapshot; no event or free-text field is executed."""
    if not data or len(data) > MAX_LOG_BYTES:
        raise LocalToolError("authentication log must be non-empty and at most 2 MiB")
    events = []
    seen = set()
    for line_number, raw in enumerate(data.split(b"\n"), 1):
        if not raw.strip():
            continue
        try:
            if len(raw) > MAX_LINE_BYTES or len(events) >= MAX_EVENTS:
                raise ValueError("log size limit")
            row = json.loads(raw.decode("utf-8"), object_pairs_hook=_object)
            if not isinstance(row, dict):
                raise ValueError("event must be an object")
            event_id = _text(row, "event_id")
            if event_id in seen:
                raise ValueError("duplicate event id")
            timestamp = datetime.fromisoformat(_text(row, "timestamp"))
            if timestamp.utcoffset() is None:
                raise ValueError("timestamp must include timezone")
            timestamp = timestamp.astimezone(timezone.utc)
            if row.get("event_type") != "authentication":
                raise ValueError("unsupported event type")
            outcome = row.get("outcome")
            if outcome not in ("success", "failure"):
                raise ValueError("unsupported outcome")
            identity = [_text(row, "user"), _text(row, "host"),
                        str(ipaddress.ip_address(_text(row, "source_ip")))]
            # Group by exact user, host and source; emit only a correlation digest.
            group = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
            events.append((timestamp, line_number, outcome, group))
            seen.add(event_id)
        except (KeyError, TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise LocalToolError(f"invalid authentication event at line {line_number}") from exc
    if not events:
        raise LocalToolError("authentication log contains no events")
    events.sort(key=lambda event: (event[0], event[1]))
    failures: dict[str, deque] = {}
    alerted: set[str] = set()
    alerts = []
    alert_count = 0
    successes = 0
    for timestamp, line, outcome, group in events:
        pending = failures.setdefault(group, deque())
        while pending and (timestamp - pending[0][0]).total_seconds() > WINDOW_SECONDS:
            pending.popleft()
        if len(pending) < FAILURE_THRESHOLD:
            alerted.discard(group)
        if outcome == "failure":
            pending.append((timestamp, line))
        else:
            successes += 1
        if len(pending) >= FAILURE_THRESHOLD and (outcome == "success" or group not in alerted):
            alert_count += 1
            if len(alerts) < MAX_ALERTS:
                evidence = [item[1] for item in pending]
                if outcome == "success":
                    evidence.append(line)
                alerts.append({
                    "rule_id": "AUTH-SUCCESS-AFTER-FAILURES" if outcome == "success" else "AUTH-FAILURE-BURST",
                    "severity": "high" if outcome == "success" else "medium",
                    "group_sha256": group,
                    "first_seen": pending[0][0].isoformat(),
                    "last_seen": timestamp.isoformat(),
                    "failure_count": len(pending),
                    "evidence_lines": evidence[:MAX_EVIDENCE_LINES - 1] + evidence[-1:]
                    if len(evidence) > MAX_EVIDENCE_LINES else evidence,
                    "evidence_line_count": len(evidence),
                    "evidence_truncated": len(evidence) > MAX_EVIDENCE_LINES,
                })
            alerted.add(group)
        if outcome == "success":
            pending.clear()
            alerted.discard(group)
    return {
        "analyzer": "jarvis-v2-auth-jsonl-v1",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "event_count": len(events),
        "success_count": successes,
        "failure_count": len(events) - successes,
        "first_seen": events[0][0].isoformat(),
        "last_seen": events[-1][0].isoformat(),
        "window_seconds": WINDOW_SECONDS,
        "failure_threshold": FAILURE_THRESHOLD,
        "alert_count": alert_count,
        "alerts": alerts,
        "alerts_truncated": alert_count > len(alerts),
        "interpretation": "Rule matches are investigation leads, not proof of intrusion.",
        "limitations": [
            "Only normalized authentication events are supported.",
            "Correlation uses the same user, host and source IP within five minutes.",
            "Password spraying across users and distributed sources is not detected.",
            "Unknown free-text fields are ignored; raw identities are omitted.",
            "Identity digests enable correlation; they are not anonymization.",
        ],
    }
