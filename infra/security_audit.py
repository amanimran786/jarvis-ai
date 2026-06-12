"""
Unified append-only security audit stream for Jarvis.

Every privileged or security-relevant action in the pipeline emits exactly one
JSONL line here: approvals and denials, threat-screen blocks, security
verdicts, cloud calls, security-tool invocations, RBAC denials, vault writes,
and rollbacks. One file, one schema, so monitoring and the security dashboard
have a single source of truth instead of fragments spread across task dicts
and per-feature logs.

Design rules:
  - Append-only JSONL at ~/.jarvis/security_audit.jsonl
    (override: JARVIS_SECURITY_AUDIT_PATH — used by tests).
  - audit_event() NEVER raises into the calling action: a broken audit disk
    must not take down voice/tasks. Failures are logged via logging.exception.
  - Every mutating action should pass rollback_ref (git branch, worktree path,
    backup file, or task_id) so any entry answers "how do I undo this?".
  - Entries carry a plain-language summary so non-technical readers can use
    the dashboard without translation.

Emit points (S2 wiring): infra/event_bus.py approvals, task_runtime
approve/deny/verdicts, infra/rbac.py denials, tools/security adapter,
agents/security_reviewer.py cloud fallback.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from infra._hashchain import (
    GENESIS, HASH_FIELD, PREV_FIELD, canonical, compute_hash, verify_chain,
)

log = logging.getLogger("jarvis.security_audit")

_LOCK = threading.Lock()

# ─── Action vocabulary ────────────────────────────────────────────────────────
# Keep this list closed-ish: dashboards group by these strings.
APPROVAL_GRANTED   = "approval_granted"
APPROVAL_DENIED    = "approval_denied"
APPROVAL_REQUESTED = "approval_requested"
THREAT_SCREEN_BLOCK = "threat_screen_block"
SECURITY_VERDICT   = "security_verdict"
CLOUD_CALL         = "cloud_call"
SECURITY_TOOL_EXEC = "security_tool_exec"
RBAC_DENY          = "rbac_deny"
VAULT_WRITE        = "vault_write"
ROLLBACK           = "rollback"

_SEVERITIES = ("info", "notice", "warning", "critical")

_HUMAN_TEMPLATES = {
    APPROVAL_GRANTED:    "{actor} approved {target}",
    APPROVAL_DENIED:     "{actor} denied {target}",
    APPROVAL_REQUESTED:  "{target} is waiting for human approval",
    THREAT_SCREEN_BLOCK: "Jarvis blocked {target} before it ran (automatic threat screen)",
    SECURITY_VERDICT:    "Security review of {target}: {decision}",
    CLOUD_CALL:          "Data was sent to an external AI service for {target}",
    SECURITY_TOOL_EXEC:  "A security tool ran against {target}",
    RBAC_DENY:           "{actor} tried something it is not allowed to do ({target})",
    VAULT_WRITE:         "Jarvis wrote to the knowledge vault: {target}",
    ROLLBACK:            "{target} was rolled back",
}


def _audit_path() -> Path:
    override = os.getenv("JARVIS_SECURITY_AUDIT_PATH", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".jarvis" / "security_audit.jsonl"


def human_line(entry: dict) -> str:
    """Plain-English one-liner for non-technical readers. Never raises."""
    try:
        action = entry.get("action", "")
        template = _HUMAN_TEMPLATES.get(action)
        actor = entry.get("actor") or "operator"
        target = entry.get("target") or entry.get("task_id") or "a task"
        decision = entry.get("decision") or "completed"
        if template:
            line = template.format(actor=actor, target=target, decision=decision)
        else:
            line = f"{actor}: {action or 'security event'} on {target}"
        reason = (entry.get("reason") or "").strip()
        if reason:
            line += f" — {reason}"
        return line
    except Exception:
        return "security event (unrenderable)"


def _tail_hash(f) -> str:
    """Last record's this_hash from an open file, or GENESIS. Reads only the
    file tail so appends stay O(1) as the ledger grows."""
    try:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 65536))
        tail = f.read()
        for raw in reversed(tail.splitlines()):
            raw = raw.strip()
            if not raw:
                continue
            try:
                return json.loads(raw).get(HASH_FIELD, GENESIS)
            except json.JSONDecodeError:
                continue  # partial first line of the tail window, or legacy junk
        return GENESIS
    except OSError:
        return GENESIS


def _append_chained(path: Path, entry: dict) -> None:
    """
    Tamper-evident append (S5): GENESIS→prev_hash→this_hash chain in the same
    format as infra/pipeline_audit (shared infra/_hashchain.py helpers).
    Unlike pipeline_audit's single-writer ledger, this stream is appended from
    multiple processes (API server, agents, app), so the tail-read + write is
    held under an exclusive flock — without it two concurrent writers would
    read the same tail hash and fork the chain.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            prev = _tail_hash(f)
            entry[PREV_FIELD] = prev
            entry[HASH_FIELD] = compute_hash(prev, entry)
            f.seek(0, os.SEEK_END)
            f.write(canonical(entry) + "\n")
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def verify_integrity() -> dict:
    """
    Verify the audit stream's hash chain. Entries written before S5 adoption
    have no chain fields — they form a 'legacy' prefix that predates
    tamper-evidence and is reported, not failed. Never raises.
    """
    try:
        path = _audit_path()
        if not path.exists():
            return {"ok": True, "checked": 0, "legacy": 0, "reason": None, "index": -1}
        entries: list[dict] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                entries.append(json.loads(raw))
            except json.JSONDecodeError:
                entries.append({"_corrupt": raw})
        legacy = 0
        while legacy < len(entries) and HASH_FIELD not in entries[legacy] \
                and "_corrupt" not in entries[legacy]:
            legacy += 1
        chained = entries[legacy:]
        ok, reason, idx = verify_chain(chained)
        return {
            "ok": ok,
            "checked": len(chained),
            "legacy": legacy,
            "reason": reason,
            "index": (legacy + idx) if idx >= 0 else -1,
        }
    except Exception:
        log.exception("security audit integrity check failed")
        return {"ok": False, "checked": 0, "legacy": 0,
                "reason": "integrity check crashed", "index": -1}


def audit_event(
    action: str,
    *,
    actor: str = "",
    target: str = "",
    decision: str = "",
    severity: str = "info",
    reason: str = "",
    task_id: str = "",
    rollback_ref: str = "",
    extra: dict | None = None,
) -> dict | None:
    """
    Append one audit entry. Returns the entry dict, or None on failure.
    NEVER raises — the audited action must not fail because auditing did.
    """
    try:
        if severity not in _SEVERITIES:
            severity = "info"
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": str(action or "")[:64],
            "actor": str(actor or "")[:128],
            "target": str(target or "")[:512],
            "decision": str(decision or "")[:64],
            "severity": severity,
            "reason": str(reason or "")[:1024],
            "task_id": str(task_id or "")[:128],
            "rollback_ref": str(rollback_ref or "")[:512],
        }
        if extra:
            # extras must never collide with or overwrite schema fields
            entry["extra"] = {str(k): str(v)[:512] for k, v in extra.items()
                              if str(k) not in entry}
        entry["summary"] = human_line(entry)
        path = _audit_path()
        with _LOCK:
            _append_chained(path, entry)
        if severity in ("warning", "critical"):
            log.warning("AUDIT %s: %s", severity, entry["summary"])
        return entry
    except Exception:
        log.exception("security audit write failed (action=%s)", action)
        return None


def read_events(
    limit: int = 200,
    action: str = "",
    severity: str = "",
    task_id: str = "",
) -> list[dict]:
    """
    Newest-first slice of the audit stream, optionally filtered.
    Reads only the tail it needs; tolerates corrupt lines. Never raises.
    """
    try:
        path = _audit_path()
        if not path.exists():
            return []
        events: list[dict] = []
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if action and entry.get("action") != action:
                continue
            if severity and entry.get("severity") != severity:
                continue
            if task_id and entry.get("task_id") != task_id:
                continue
            events.append(entry)
            if len(events) >= max(1, int(limit)):
                break
        return events
    except Exception:
        log.exception("security audit read failed")
        return []


def summarize(limit: int = 1000) -> dict:
    """
    Aggregate view over the most recent entries, shaped for the dashboard:
    counts by action / severity / decision, the latest critical items, and
    plain-language lines for the non-technical panel. Never raises.
    """
    events = read_events(limit=limit)
    by_action: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    denials = 0
    blocked = 0
    cloud_calls = 0
    rollbackable = 0
    recent_critical: list[dict] = []
    for e in events:
        by_action[e.get("action", "?")] = by_action.get(e.get("action", "?"), 0) + 1
        by_severity[e.get("severity", "?")] = by_severity.get(e.get("severity", "?"), 0) + 1
        if e.get("action") in (APPROVAL_DENIED, RBAC_DENY):
            denials += 1
        if e.get("action") == THREAT_SCREEN_BLOCK:
            blocked += 1
        if e.get("action") == CLOUD_CALL:
            cloud_calls += 1
        if e.get("rollback_ref"):
            rollbackable += 1
        if e.get("severity") == "critical" and len(recent_critical) < 10:
            recent_critical.append(e)
    return {
        "total": len(events),
        "by_action": by_action,
        "by_severity": by_severity,
        "denials": denials,
        "threat_blocks": blocked,
        "cloud_calls": cloud_calls,
        "rollbackable": rollbackable,
        "recent_critical": recent_critical,
        "plain_language": [e.get("summary") or human_line(e) for e in events[:20]],
        "integrity": verify_integrity(),
    }
