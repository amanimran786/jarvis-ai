"""
proactive_watcher.py — Proactive notification watcher.

Background thread polling:
  - Google Calendar: events starting in the next 10 minutes (every 5 min)
  - Gmail: urgent unread emails (every 5 min)

Alerts accumulate in an in-memory buffer exposed via get_alerts().
Surfaced as GET /alerts in api.py.

Configuration (env vars):
  JARVIS_PROACTIVE_POLL_SEC        — poll interval in seconds (default 300)
  JARVIS_PROACTIVE_CAL_LOOKAHEAD   — calendar lookahead in minutes (default 10)
  JARVIS_PROACTIVE_ENABLED         — "0" to disable

Public API:
  start()                       — start the watcher (idempotent)
  stop()                        — request clean shutdown
  status() -> dict              — current watcher state
  get_alerts() -> list[dict]    — current non-dismissed alerts
  dismiss_alert(id: str) -> bool — dismiss an alert by id
"""

from __future__ import annotations

import os
import re
import threading
import time
import logging
import uuid
import datetime
from datetime import timezone


# ── Configuration ─────────────────────────────────────────────────────────────

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


_POLL_SEC      = _int_env("JARVIS_PROACTIVE_POLL_SEC", 300)       # 5 min
_CAL_LOOKAHEAD = _int_env("JARVIS_PROACTIVE_CAL_LOOKAHEAD", 10)   # 10 min
_ENABLED       = os.getenv("JARVIS_PROACTIVE_ENABLED", "1").strip() not in {"0", "false", "no", "off"}
_MAX_BUFFER    = 100


# ── State ─────────────────────────────────────────────────────────────────────

_buf_lock   = threading.Lock()
_ctl_lock   = threading.Lock()
_thread: threading.Thread | None = None
_stop_event = threading.Event()
_run_count  = 0
_last_run: float = 0.0

_alerts: list[dict] = []
_seen_keys: set[str] = set()


# ── Alert buffer ──────────────────────────────────────────────────────────────

def _push(kind: str, key: str, message: str) -> dict | None:
    """Add an alert; skip if key already seen. Returns the new alert or None."""
    if key in _seen_keys:
        return None
    _seen_keys.add(key)
    alert: dict = {
        "id":        str(uuid.uuid4()),
        "kind":      kind,
        "key":       key,
        "message":   message,
        "timestamp": datetime.datetime.now().isoformat(),
        "dismissed": False,
    }
    with _buf_lock:
        _alerts.append(alert)
        # Evict oldest dismissed entry when buffer is full
        if len(_alerts) > _MAX_BUFFER:
            for i, a in enumerate(_alerts):
                if a["dismissed"]:
                    _alerts.pop(i)
                    break
    return alert


def get_alerts(include_dismissed: bool = False) -> list[dict]:
    """Return current alerts (active only by default)."""
    with _buf_lock:
        if include_dismissed:
            return list(_alerts)
        return [a for a in _alerts if not a["dismissed"]]


def dismiss_alert(alert_id: str) -> bool:
    """Mark an alert as dismissed. Returns True if found."""
    with _buf_lock:
        for a in _alerts:
            if a["id"] == alert_id:
                a["dismissed"] = True
                return True
    return False


# ── Urgency pattern ────────────────────────────────────────────────────────────

_URGENT_RE = re.compile(
    r"(urgent|action required|immediate|asap|time.sensitive|critical|response needed|"
    r"deadline|overdue|important|attention|please respond|follow.up required)",
    re.I,
)


# ── Checks ────────────────────────────────────────────────────────────────────

def _check_calendar() -> None:
    """Alert for calendar events starting within _CAL_LOOKAHEAD minutes."""
    try:
        import google_services as gs
        now = datetime.datetime.now(timezone.utc)
        horizon = now + datetime.timedelta(minutes=_CAL_LOOKAHEAD)
        result = gs._calendar().events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=horizon.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=20,
        ).execute()
        for e in result.get("items", []):
            title = e.get("summary", "Untitled event")
            start_str = e["start"].get("dateTime", e["start"].get("date", ""))
            key = f"cal:{start_str[:19]}:{title[:50]}"
            if "T" in start_str:
                start_dt = datetime.datetime.fromisoformat(start_str)
                minutes_away = max(0, int((start_dt - now).total_seconds() // 60))
                msg = f"'{title}' starts in {minutes_away} min"
            else:
                msg = f"'{title}' (all day)"
            alert = _push("calendar", key, msg)
            if alert:
                _send_notification("Jarvis — Calendar", msg)
    except Exception:
        logging.debug("[ProactiveWatcher] Calendar check failed", exc_info=True)


def _check_emails() -> None:
    """Alert for urgent unread Gmail messages."""
    try:
        import google_services as gs
        for e in gs.get_unread_email_subjects(max_results=10):
            combined = e.get("subject", "") + " " + e.get("snippet", "")
            if not _URGENT_RE.search(combined):
                continue
            sender = e.get("sender", "Unknown")
            subject = e.get("subject", "")
            key = f"email:{sender[:30]}:{subject[:50]}"
            msg = f"Urgent email from {sender}: {subject}"
            alert = _push("email", key, msg)
            if alert:
                _send_notification("Jarvis — Email", msg)
    except Exception:
        logging.debug("[ProactiveWatcher] Email check failed", exc_info=True)


def _send_notification(title: str, body: str) -> None:
    try:
        import jarvis_watcher as _jw
        _jw.notify(title, body)
    except Exception:
        logging.debug("[ProactiveWatcher] Desktop notification failed", exc_info=True)


# ── Poll loop ─────────────────────────────────────────────────────────────────

def _loop() -> None:
    global _run_count, _last_run
    while not _stop_event.is_set():
        _stop_event.wait(timeout=_POLL_SEC)
        if _stop_event.is_set():
            break
        with _ctl_lock:
            _run_count += 1
            _last_run = time.monotonic()
        _check_calendar()
        _check_emails()


# ── Public API ────────────────────────────────────────────────────────────────

def start() -> None:
    """Start the proactive watcher thread (idempotent)."""
    global _thread
    if not _ENABLED:
        return
    with _ctl_lock:
        if _thread and _thread.is_alive():
            return
        _stop_event.clear()
        _thread = threading.Thread(target=_loop, name="jarvis-proactive", daemon=True)
        _thread.start()


def stop() -> None:
    """Request a clean shutdown."""
    _stop_event.set()


def status() -> dict:
    with _ctl_lock:
        running = bool(_thread and _thread.is_alive())
        rc = _run_count
    with _buf_lock:
        active = sum(1 for a in _alerts if not a["dismissed"])
        total = len(_alerts)
    return {
        "enabled":                _ENABLED,
        "running":                running,
        "poll_interval_sec":      _POLL_SEC,
        "calendar_lookahead_min": _CAL_LOOKAHEAD,
        "run_count":              rc,
        "active_alerts":          active,
        "total_alerts":           total,
        "seen_keys":              len(_seen_keys),
    }
