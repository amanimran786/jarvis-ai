"""
Jarvis Proactive Agent System.

Agents run continuously in the background and surface alerts without
being asked. Each agent has an interval, a run() method, and an
optional priority level that determines whether Jarvis speaks the alert.

Built-in agents:
  EmailAgent        — surfaces new important emails every 5 min
  MeetingPrepAgent  — briefs on the next meeting 10 min before it starts
  SystemHealthAgent — alerts on high CPU / low disk / memory pressure
  ResearchAgent     — proactively surfaces relevant news/insights
  IdleContextAgent  — suggests useful actions during quiet periods

Usage:
  import agents
  agents.start(on_alert=my_callback)   # on_alert(title, body, speak: bool)
  agents.stop()
"""

import threading
import time
import os
import subprocess
from datetime import datetime, timezone, timedelta
from abc import ABC, abstractmethod

from provider_priority import ask_with_priority
import memory as mem

# ── Base agent ────────────────────────────────────────────────────────────────

class Agent(ABC):
    name: str = "Agent"
    interval: int = 300          # seconds between runs
    speak_alerts: bool = False   # True = Jarvis voices this alert
    enabled: bool = True

    def __init__(self):
        self._last_run: float = time.time()  # wait full interval before first run
        self._state: dict = {}   # persistent per-agent state

    def due(self) -> bool:
        return (time.time() - self._last_run) >= self.interval

    def tick(self, on_alert):
        if not self.enabled or not self.due():
            return
        self._last_run = time.time()
        try:
            result = self.run()
            if result:
                title, body, speak = result
                on_alert(title, body, speak)
        except Exception as e:
            print(f"[Agent:{self.name}] Error: {e}")

    @abstractmethod
    def run(self) -> tuple[str, str, bool] | None:
        """Return (title, body, speak_aloud) or None if nothing to surface."""
        ...


# ── Email agent ────────────────────────────────────────────────────────────────

class EmailAgent(Agent):
    name = "Email"
    interval = 300       # 5 minutes
    speak_alerts = False

    def __init__(self):
        super().__init__()
        self._seen_ids: set = set()
        self._initialized = False

    def run(self):
        try:
            import google_services as gs
            # Get raw message IDs — we need the Gmail client directly
            from google_services import _gmail
            result = _gmail().users().messages().list(
                userId="me", labelIds=["INBOX", "UNREAD"], maxResults=10
            ).execute()
            messages = result.get("messages", [])
            new_ids = {m["id"] for m in messages}

            if not self._initialized:
                self._seen_ids = new_ids
                self._initialized = True
                return None

            fresh = new_ids - self._seen_ids
            if not fresh:
                return None

            self._seen_ids = new_ids

            # Fetch subject/sender for new messages
            summaries = []
            for mid in list(fresh)[:3]:
                detail = _gmail().users().messages().get(
                    userId="me", id=mid, format="metadata",
                    metadataHeaders=["From", "Subject"]
                ).execute()
                headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
                sender = headers.get("From", "Unknown").split("<")[0].strip()
                subject = headers.get("Subject", "No subject")
                summaries.append(f"{sender}: {subject}")

            count = len(fresh)
            body = f"{count} new email{'s' if count > 1 else ''}. " + " | ".join(summaries)
            speak = count >= 1 and any(
                kw in body.lower() for kw in
                ["urgent", "asap", "important", "action required", "interview", "offer"]
            )
            return "📧 New Email", body, speak

        except Exception as e:
            return None


# ── Meeting prep agent ─────────────────────────────────────────────────────────

class MeetingPrepAgent(Agent):
    name = "MeetingPrep"
    interval = 60        # check every minute
    speak_alerts = True

    def __init__(self):
        super().__init__()
        self._alerted_events: set = set()

    def run(self):
        try:
            import google_services as gs
            from google_services import _calendar

            now = datetime.now(timezone.utc)
            window_start = now.isoformat()
            window_end = (now + timedelta(minutes=15)).isoformat()

            result = _calendar().events().list(
                calendarId="primary",
                timeMin=window_start,
                timeMax=window_end,
                singleEvents=True,
                orderBy="startTime"
            ).execute()

            events = result.get("items", [])
            for event in events:
                eid = event.get("id", "")
                if eid in self._alerted_events:
                    continue

                start_str = event["start"].get("dateTime", "")
                if not start_str:
                    continue

                start_dt = datetime.fromisoformat(start_str)
                mins_away = int((start_dt - now).total_seconds() / 60)

                if 8 <= mins_away <= 12:   # 10-min window
                    self._alerted_events.add(eid)
                    title = event.get("summary", "Meeting")
                    attendees = event.get("attendees", [])
                    attendee_names = [a.get("email", "").split("@")[0] for a in attendees[:3]]

                    # Generate a prep brief
                    facts = mem.list_facts()
                    context = "\n".join(f"- {f}" for f in facts[:5]) if facts else "No context available."
                    prompt = (
                        f"Jarvis is briefing the user 10 minutes before: '{title}'\n"
                        f"Attendees: {', '.join(attendee_names) or 'unknown'}\n"
                        f"User context:\n{context}\n\n"
                        f"Give a 2-sentence prep brief: what to expect and one smart talking point. "
                        f"Spoken aloud — no markdown."
                    )
                    brief = ask_with_priority(prompt, tier="cheap")
                    return f"📅 {title} in ~{mins_away} min", brief, True

        except Exception:
            pass
        return None


# ── System health agent ────────────────────────────────────────────────────────

class SystemHealthAgent(Agent):
    name = "SystemHealth"
    interval = 600       # 10 minutes
    speak_alerts = False

    def run(self):
        alerts = []

        # CPU load (1-min average via uptime)
        try:
            out = subprocess.run(["uptime"], capture_output=True, text=True).stdout
            # macOS uptime format: "... load averages: 1.23 2.34 3.45"
            parts = out.split("load averages:")
            if len(parts) > 1:
                load_1m = float(parts[1].strip().split()[0])
                cpu_count = os.cpu_count() or 4
                if load_1m > cpu_count * 0.9:
                    alerts.append(f"CPU load high: {load_1m:.1f} ({cpu_count} cores)")
        except Exception:
            pass

        # Disk space
        try:
            result = subprocess.run(
                ["df", "-h", os.path.expanduser("~")],
                capture_output=True, text=True
            )
            lines = result.stdout.strip().split("\n")
            if len(lines) > 1:
                parts = lines[1].split()
                use_pct = int(parts[4].replace("%", ""))
                if use_pct >= 90:
                    alerts.append(f"Disk {use_pct}% full ({parts[3]} free)")
        except Exception:
            pass

        # Memory pressure (macOS vm_stat)
        try:
            vm = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
            pages_free = 0
            pages_spec = 0
            for line in vm.split("\n"):
                if "Pages free" in line:
                    pages_free = int(line.split(":")[1].strip().rstrip("."))
                if "Pages speculative" in line:
                    pages_spec = int(line.split(":")[1].strip().rstrip("."))
            free_mb = (pages_free + pages_spec) * 4096 / 1024 / 1024
            if free_mb < 512:
                alerts.append(f"Memory low: {free_mb:.0f}MB free")
        except Exception:
            pass

        if alerts:
            body = " | ".join(alerts)
            return "⚠️ System Health", body, False

        return None


# ── Research / insight agent ───────────────────────────────────────────────────

class ResearchAgent(Agent):
    name = "Research"
    interval = 14400     # 4 hours

    def run(self):
        try:
            from learner import _load_knowledge
            data = _load_knowledge()
            feed = data.get("knowledge_feed", [])
            if not feed:
                return None

            # Find the most recent item not yet surfaced
            surfaced = self._state.get("surfaced", set())
            for item in feed:
                key = item.get("summary", "")[:60]
                if key not in surfaced:
                    self._state["surfaced"] = surfaced | {key}
                    topic = item.get("topic", "a topic you follow")
                    summary = item.get("summary", "")
                    if len(summary) > 20:
                        return f"📡 Intel: {topic}", summary, False
        except Exception:
            pass
        return None


# ── Idle context agent ─────────────────────────────────────────────────────────

class IdleContextAgent(Agent):
    name = "IdleContext"
    interval = 1800      # 30 minutes

    def __init__(self):
        super().__init__()
        self._last_suggestion_hour = -1

    def run(self):
        now = datetime.now()
        hour = now.hour

        # Only once per hour, and only during waking hours
        if hour == self._last_suggestion_hour or hour < 8 or hour > 22:
            return None

        try:
            top_topics = mem.get_top_topics(3)
            facts = mem.list_facts()
            recent = mem.get_recent_conversations(2)

            context = []
            if facts:
                context.append("User facts: " + "; ".join(facts[:3]))
            if top_topics:
                context.append("Frequent topics: " + ", ".join(top_topics))
            if recent:
                context.append("Recent: " + recent[-1].get("summary", ""))

            time_of_day = (
                "morning" if hour < 12 else
                "afternoon" if hour < 17 else
                "evening"
            )

            prompt = (
                f"It's {time_of_day}. Jarvis wants to proactively surface one useful thing "
                f"for the user based on context:\n"
                f"{chr(10).join(context)}\n\n"
                f"Give one short, specific, actionable suggestion or insight — 1-2 sentences. "
                f"No filler. Spoken aloud."
            )
            suggestion = ask_with_priority(prompt, tier="cheap")
            self._last_suggestion_hour = hour
            return f"💡 {time_of_day.capitalize()} Insight", suggestion, False

        except Exception:
            pass
        return None


# ── Runner ─────────────────────────────────────────────────────────────────────

_running = False
_thread: threading.Thread | None = None
_on_alert_cb = None

_AGENTS: list[Agent] = [
    EmailAgent(),
    MeetingPrepAgent(),
    SystemHealthAgent(),
    ResearchAgent(),
    IdleContextAgent(),
]


def start(on_alert=None):
    """
    Start all agents in a single background thread.
    on_alert(title: str, body: str, speak: bool)
    """
    global _running, _thread, _on_alert_cb
    if _running:
        return

    _on_alert_cb = on_alert
    _running = True

    def _loop():
        print(f"[Agents] Running {len(_AGENTS)} proactive agents.")
        while _running:
            for agent in _AGENTS:
                if not _running:
                    break
                agent.tick(_fire)
            time.sleep(10)   # tick resolution

    _thread = threading.Thread(target=_loop, daemon=True, name="AgentRunner")
    _thread.start()


def stop():
    global _running
    _running = False


def _fire(title: str, body: str, speak: bool):
    if _on_alert_cb:
        _on_alert_cb(title, body, speak)


def get_agents() -> list[Agent]:
    return _AGENTS


def set_enabled(name: str, enabled: bool):
    for a in _AGENTS:
        if a.name.lower() == name.lower():
            a.enabled = enabled
            return True
    return False
