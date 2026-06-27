"""
jarvis_watcher.py — Proactive background watcher for Iron Man Jarvis.

Runs a low-frequency background loop that:
  1. Scans for calendar events starting in the next N minutes
  2. Checks the task hub for urgent/overdue open tasks
  3. When something actionable is found, surfaces it via:
       a. macOS banner notification
       b. Jarvis TTS (if not already speaking and user is likely present)

This is the "Jarvis is in control" layer — it acts without being asked
whenever something truly needs Aman's attention.

Configuration (env vars or config defaults):
  JARVIS_WATCHER_INTERVAL_SEC   — how often to run the check (default 300 = 5 min)
  JARVIS_WATCHER_QUIET_START    — quiet hours start, 24h (default "22")  HH
  JARVIS_WATCHER_QUIET_END      — quiet hours end,   24h (default "08")  HH
  JARVIS_WATCHER_ENABLED        — "0" to disable the watcher entirely

Public API
──────────
  start()                 — start the watcher thread (idempotent)
  stop()                  — request clean shutdown
  status() -> dict        — current watcher state
  notify(title, body)     — one-shot macOS notification (callable by other modules)
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
import datetime
from typing import Callable

# ── Configuration ─────────────────────────────────────────────────────────────

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


_INTERVAL_SEC    = _int_env("JARVIS_WATCHER_INTERVAL_SEC", 300)   # 5 min
_QUIET_START_H   = _int_env("JARVIS_WATCHER_QUIET_START", 22)     # 10 PM
_QUIET_END_H     = _int_env("JARVIS_WATCHER_QUIET_END",    8)     #  8 AM
_ENABLED         = os.getenv("JARVIS_WATCHER_ENABLED", "1").strip() not in {"0", "false", "no", "off"}

# How many minutes ahead to warn for calendar events
_CALENDAR_WARN_MIN = 15

# ── State ─────────────────────────────────────────────────────────────────────

_lock            = threading.Lock()
_thread: threading.Thread | None = None
_stop_event      = threading.Event()
_last_run: float = 0.0
_last_escalation: str = ""
_run_count: int  = 0

# Set of already-notified event/task keys so we don't repeat the same alert
_notified_keys: set[str] = set()

# ── Ambient triggers (ADK-style event-driven ambient agents) ──────────────────
# Each trigger is a named function that fires when its condition is met.
# Triggers run inside the watcher loop — they fire independently of the
# 5-minute polling interval when their specific condition becomes true.

_trigger_registry: dict[str, "AmbientTrigger"] = {}


class AmbientTrigger:
    """A named trigger that fires when its condition function returns True.

    Designed after ADK LoopAgent: check() is called every loop iteration;
    fire() runs the action; cool_down prevents re-firing too soon.
    """
    def __init__(self, name: str, check_fn: Callable[[], bool],
                 fire_fn: Callable[[], None], cool_down: int = 300):
        self.name = name
        self._check = check_fn
        self._fire = fire_fn
        self.cool_down = cool_down
        self._last_fired: float = 0.0

    def poll(self) -> bool:
        """Check condition and fire if met and not in cooldown. Returns True if fired."""
        if time.monotonic() - self._last_fired < self.cool_down:
            return False
        try:
            if self._check():
                self._fire()
                self._last_fired = time.monotonic()
                return True
        except Exception:
            logging.debug("[Watcher] trigger %s check/fire failed", self.name, exc_info=True)
        return False


def register_trigger(trigger: AmbientTrigger) -> None:
    _trigger_registry[trigger.name] = trigger


def _poll_triggers() -> None:
    for trigger in list(_trigger_registry.values()):
        try:
            trigger.poll()
        except Exception:
            logging.debug("[Watcher] trigger poll failed for %s", trigger.name, exc_info=True)

# Optional callback: when the watcher wants to speak, it calls this.
# Set by main.py / ui.py via set_speak_callback().
_speak_cb: Callable[[str], None] | None = None


def set_speak_callback(fn: Callable[[str], None]) -> None:
    """Register a TTS callable. The watcher will use it for proactive alerts."""
    global _speak_cb
    _speak_cb = fn


# ── macOS notifications ────────────────────────────────────────────────────────

def notify(title: str, body: str, subtitle: str = "") -> None:
    """Send a macOS banner notification. Delegates to harness.notify."""
    from harness.notify import notify as _hnotify
    _hnotify(title, body, subtitle=subtitle)


def _osa_escape(text: str) -> str:
    """Escape a string for use inside an AppleScript double-quoted string."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


# ── Quiet-hours check ─────────────────────────────────────────────────────────

def _is_quiet_hours() -> bool:
    h = datetime.datetime.now().hour
    if _QUIET_START_H > _QUIET_END_H:
        # e.g., 22 to 08 wraps midnight
        return h >= _QUIET_START_H or h < _QUIET_END_H
    return _QUIET_START_H <= h < _QUIET_END_H


# ── Calendar scan ─────────────────────────────────────────────────────────────

def _check_calendar() -> list[tuple[str, str]]:
    """Return list of (key, message) for events starting within _CALENDAR_WARN_MIN minutes."""
    alerts: list[tuple[str, str]] = []
    try:
        import google_services as gs
        if not hasattr(gs, "get_todays_events"):
            return alerts
        events = gs.get_todays_events()
        if not events:
            return alerts
        now = datetime.datetime.now()
        warn_delta = datetime.timedelta(minutes=_CALENDAR_WARN_MIN)
        for event in events:
            event_str = str(event)
            # Try to extract a time from the event string (e.g. "10:30 AM - Meeting")
            time_match = re.search(r"\b(\d{1,2}):(\d{2})\s*(AM|PM)?\b", event_str, re.I)
            if not time_match:
                continue
            h = int(time_match.group(1))
            m = int(time_match.group(2))
            ampm = (time_match.group(3) or "").upper()
            if ampm == "PM" and h < 12:
                h += 12
            elif ampm == "AM" and h == 12:
                h = 0
            event_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
            delta = event_dt - now
            if datetime.timedelta(0) <= delta <= warn_delta:
                key = f"cal:{event_str[:60]}"
                if key not in _notified_keys:
                    alerts.append((key, f"Starting in {int(delta.total_seconds() // 60)} min: {event_str}"))
    except Exception:
        logging.debug("[Watcher] calendar scan failed", exc_info=True)
    return alerts


# ── Email scan ────────────────────────────────────────────────────────────────

_EMAIL_URGENT_PATTERNS = re.compile(
    r"(urgent|action required|immediate|asap|time.sensitive|critical|response needed|"
    r"deadline|overdue|important|attention|please respond|follow.up required)",
    re.I,
)


def _check_emails() -> list[tuple[str, str]]:
    """Return list of (key, message) for unread emails with urgency signals."""
    alerts: list[tuple[str, str]] = []
    try:
        import google_services as gs
        if not hasattr(gs, "get_unread_email_subjects"):
            return alerts
        emails = gs.get_unread_email_subjects(max_results=10)
        for e in emails:
            combined = e.get("subject", "") + " " + e.get("snippet", "")
            if _EMAIL_URGENT_PATTERNS.search(combined):
                key = f"email:{e['sender'][:30]}:{e['subject'][:50]}"
                if key not in _notified_keys:
                    msg = f"Urgent email from {e['sender']}: {e['subject']}"
                    alerts.append((key, msg))
    except Exception:
        logging.debug("[Watcher] email scan failed", exc_info=True)
    return alerts


# ── Task scan ─────────────────────────────────────────────────────────────────

_URGENT_PATTERNS = re.compile(
    r"(urgent|overdue|blocked|action required|attention|high priority|today|critical|asap)",
    re.I,
)


def _needs_escalation(text: str) -> bool:
    """Return True if text contains an urgency keyword that warrants proactive alerting."""
    return bool(_URGENT_PATTERNS.search(text))


def _check_tasks() -> list[tuple[str, str]]:
    """Return list of (key, message) for urgent open tasks."""
    alerts: list[tuple[str, str]] = []
    try:
        import vault_capture
        if not hasattr(vault_capture, "read_note"):
            return alerts
        result = vault_capture.read_note("90 Task Hub", max_chars=2000)
        if isinstance(result, dict):
            content = result.get("content", "")
        else:
            content = str(result) if result else ""
        for line in content.splitlines():
            if "- [ ]" not in line:
                continue
            if _needs_escalation(line):
                clean = line.strip().lstrip("-").strip()
                key = f"task:{clean[:80]}"
                if key not in _notified_keys:
                    alerts.append((key, f"Urgent task: {clean[:120]}"))
    except Exception:
        logging.debug("[Watcher] task scan failed", exc_info=True)
    return alerts


# ── Alert delivery ─────────────────────────────────────────────────────────────

def _deliver_alerts(alerts: list[tuple[str, str]]) -> None:
    """Send notifications and optionally speak for a batch of alerts."""
    global _last_escalation
    for key, message in alerts:
        _notified_keys.add(key)
        notify("Jarvis", message)

    if not alerts:
        return

    _last_escalation = alerts[0][1]

    if _is_quiet_hours():
        return

    if _speak_cb is not None:
        if len(alerts) == 1:
            speech = f"Heads up. {alerts[0][1]}"
        else:
            speech = f"You have {len(alerts)} items that need attention. " + ". ".join(
                m for _, m in alerts[:3]
            )
        try:
            _speak_cb(speech)
        except Exception:
            logging.warning("[Watcher] TTS alert delivery failed", exc_info=True)


# ── Morning brief ─────────────────────────────────────────────────────────────

_MORNING_BRIEF_HOUR  = _int_env("JARVIS_MORNING_BRIEF_HOUR", 8)    # 8 AM default
_MORNING_BRIEF_WINDOW = 10                                           # fire within 10-min window
_morning_brief_date: datetime.date | None = None                    # tracks last delivery date


def _should_deliver_morning_brief() -> bool:
    """Return True once per day inside the configured morning window."""
    global _morning_brief_date
    now = datetime.datetime.now()
    today = now.date()
    if _morning_brief_date == today:
        return False
    if now.hour == _MORNING_BRIEF_HOUR and now.minute < _MORNING_BRIEF_WINDOW:
        return True
    return False


def _deliver_morning_brief() -> None:
    global _morning_brief_date
    _morning_brief_date = datetime.datetime.now().date()
    try:
        import jarvis_agents as _ja
        brief = _ja.run_briefing()

        # Write the daily note to vault (fire-and-forget, never blocks brief)
        try:
            focus = _ja.focus_advisor()
            note_result = _ja.write_daily_note(briefing_text=brief, focus_text=focus)
            if note_result.get("ok") and note_result.get("action") == "created":
                _note_path = note_result.get("path", "")
                notify(
                    "Jarvis — Daily Note",
                    f"Today's note created: {_note_path.split('/')[-1]}",
                )
        except Exception:
            logging.debug("[Watcher] daily note write failed", exc_info=True)

        notify("Jarvis — Morning Brief", "Your daily briefing is ready.")
        if _speak_cb is not None and not _is_quiet_hours():
            try:
                _speak_cb(brief)
            except Exception:
                logging.warning("[Watcher] TTS morning brief speak failed", exc_info=True)
    except Exception:
        logging.warning("[Watcher] morning brief delivery failed", exc_info=True)


# ── End-of-day summary ────────────────────────────────────────────────────────

_EOD_HOUR   = _int_env("JARVIS_EOD_HOUR", 18)      # 6 PM default
_EOD_WINDOW = 10                                    # fire within 10-min window
_eod_date: datetime.date | None = None              # tracks last delivery date

_EOD_SYSTEM = (
    "You are Jarvis. Give a brief end-of-day summary in 3-4 spoken sentences. "
    "Mention what's still open on the task list, any unfinished items from today, "
    "and one clear thing to prioritise first thing tomorrow. "
    "Sound calm, direct, and like you're closing out the day. Under 80 words. No bullet points."
)


def _should_deliver_eod() -> bool:
    """Return True once per day inside the configured end-of-day window."""
    global _eod_date
    now = datetime.datetime.now()
    today = now.date()
    if _eod_date == today:
        return False
    if now.hour == _EOD_HOUR and now.minute < _EOD_WINDOW:
        return True
    return False


def _deliver_eod_summary() -> None:
    global _eod_date
    _eod_date = datetime.datetime.now().date()
    try:
        import jarvis_agents as _ja
        import model_router as _mr

        # Pull tasks and calendar for tomorrow via agents
        results = _ja.dispatch_parallel(["tasks", "calendar"])
        raw = _ja.escalation_summary()  # surface anything still urgent
        if not raw or raw.startswith("Nothing"):
            raw = _ja._merge_results(results) or "No open tasks or events found."
        eod_text = _ja._synthesise(raw, system=_EOD_SYSTEM)

        notify("Jarvis — End of Day", "Closing out — your EOD summary is ready.")
        if _speak_cb is not None and not _is_quiet_hours():
            try:
                _speak_cb(eod_text)
            except Exception:
                logging.warning("[Watcher] TTS EOD summary speak failed", exc_info=True)
    except Exception:
        logging.warning("[Watcher] EOD summary delivery failed", exc_info=True)


# ── Watcher loop ──────────────────────────────────────────────────────────────

def _watcher_loop() -> None:
    global _last_run, _run_count
    while not _stop_event.is_set():
        _stop_event.wait(timeout=_INTERVAL_SEC)
        if _stop_event.is_set():
            break
        with _lock:
            _last_run = time.monotonic()
            _run_count += 1

        # Morning brief fires once per day at the configured hour
        try:
            if _should_deliver_morning_brief():
                _deliver_morning_brief()
        except Exception:
            logging.warning("[Watcher] morning brief loop failed", exc_info=True)

        # End-of-day summary fires once per day at the configured hour
        try:
            if _should_deliver_eod():
                _deliver_eod_summary()
        except Exception:
            logging.warning("[Watcher] EOD summary loop failed", exc_info=True)

        alerts: list[tuple[str, str]] = []
        try:
            alerts.extend(_check_calendar())
            alerts.extend(_check_tasks())
            alerts.extend(_check_emails())
        except Exception:
            logging.warning("[Watcher] alert check loop failed", exc_info=True)

        # Health monitor — surface newly degraded components (once per session)
        try:
            import jarvis_health as _jh
            bad = _jh.degraded()
            for component in bad:
                key = f"health:{component}"
                if key not in _notified_keys:
                    _notified_keys.add(key)
                    notify("Jarvis — System Alert", f"{component.title()} is degraded. Say 'health check' for details.")
        except Exception:
            logging.warning("[Watcher] health monitor check failed", exc_info=True)

        if alerts:
            try:
                _deliver_alerts(alerts)
            except Exception:
                logging.warning("[Watcher] alert delivery failed", exc_info=True)

        # Ambient triggers — fire independently on their own conditions
        try:
            _poll_triggers()
        except Exception:
            logging.warning("[Watcher] trigger poll loop failed", exc_info=True)


# ── Built-in ambient triggers ─────────────────────────────────────────────────

def _vault_new_note_check() -> bool:
    """Return True if a vault note was modified/created since the last check."""
    try:
        import vault
        vault_root = vault.VAULT_ROOT
        cutoff = time.time() - _INTERVAL_SEC * 1.5
        for p in vault_root.rglob("*.md"):
            if p.stat().st_mtime > cutoff:
                return True
    except Exception:
        logging.debug("[Watcher] vault new-note check failed", exc_info=True)
    return False


def _vault_new_note_fire() -> None:
    """Re-index the vault and notify if new notes were found."""
    try:
        import vault
        result = vault.refresh_index()
        added = result.get("added", 0)
        if added > 0:
            notify("Jarvis — Vault", f"{added} new note{'s' if added != 1 else ''} indexed in the brain.")
    except Exception:
        logging.debug("[Watcher] vault re-index notification failed", exc_info=True)


def _multica_new_issue_check() -> bool:
    """Return True if there are unreviewed Multica issues assigned to an agent."""
    try:
        import multica_tools as mt
        issues = mt.list_issues(status="todo") or []
        # Fire if there are unassigned todo issues
        return any(not i.get("assignee_id") for i in issues if isinstance(i, dict))
    except Exception:
        logging.debug("[Watcher] Multica issue check failed", exc_info=True)
    return False


def _multica_new_issue_fire() -> None:
    """Alert about unassigned Multica board issues."""
    try:
        import multica_tools as mt
        issues = mt.list_issues(status="todo") or []
        unassigned = [i for i in issues if isinstance(i, dict) and not i.get("assignee_id")]
        if unassigned:
            titles = ", ".join(i.get("title", i.get("id", "?")) for i in unassigned[:3])
            notify("Jarvis — Board", f"{len(unassigned)} unassigned issue{'s' if len(unassigned)!=1 else ''}: {titles}")
    except Exception:
        logging.debug("[Watcher] Multica board notification failed", exc_info=True)


# Register built-in triggers (they activate when the watcher starts)
register_trigger(AmbientTrigger(
    name="vault_new_note",
    check_fn=_vault_new_note_check,
    fire_fn=_vault_new_note_fire,
    cool_down=120,   # at most once every 2 minutes
))

register_trigger(AmbientTrigger(
    name="multica_unassigned",
    check_fn=_multica_new_issue_check,
    fire_fn=_multica_new_issue_fire,
    cool_down=600,   # at most once every 10 minutes
))


# ── Public API ────────────────────────────────────────────────────────────────

def start() -> None:
    """Start the watcher background thread (idempotent)."""
    global _thread
    if not _ENABLED:
        return
    with _lock:
        if _thread and _thread.is_alive():
            return
        _stop_event.clear()
        _thread = threading.Thread(
            target=_watcher_loop,
            name="jarvis-watcher",
            daemon=True,
        )
        _thread.start()


def stop() -> None:
    """Request a clean shutdown of the watcher thread."""
    _stop_event.set()


def status() -> dict:
    """Return current watcher state."""
    return {
        "enabled":               _ENABLED,
        "running":               bool(_thread and _thread.is_alive()),
        "interval_sec":          _INTERVAL_SEC,
        "quiet_start_hour":      _QUIET_START_H,
        "quiet_end_hour":        _QUIET_END_H,
        "run_count":             _run_count,
        "last_escalation":       _last_escalation,
        "notified_count":        len(_notified_keys),
        "morning_brief_hour":    _MORNING_BRIEF_HOUR,
        "morning_brief_sent":    _morning_brief_date.isoformat() if _morning_brief_date else None,
        "eod_hour":              _EOD_HOUR,
        "eod_sent":              _eod_date.isoformat() if _eod_date else None,
        "triggers":              {
            name: {"cool_down": t.cool_down, "last_fired": t._last_fired}
            for name, t in _trigger_registry.items()
        },
    }
