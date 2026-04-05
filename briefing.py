import json
import os
from datetime import datetime, timezone

SESSION_FILE = os.path.join(os.path.dirname(__file__), "last_session.json")
BRIEFING_THRESHOLD_HOURS = 3  # give a briefing if away for more than this


def _save_session():
    with open(SESSION_FILE, "w") as f:
        json.dump({"last_seen": datetime.now(timezone.utc).isoformat()}, f)


def should_brief() -> bool:
    """Return True if enough time has passed since the last session."""
    result = True
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE) as f:
                data = json.load(f)
            last = datetime.fromisoformat(data["last_seen"])
            hours_away = (datetime.now(timezone.utc) - last).total_seconds() / 3600
            result = hours_away >= BRIEFING_THRESHOLD_HOURS
        except Exception:
            result = True
    _save_session()  # update timestamp after checking
    return result


def _greeting() -> str:
    hour = datetime.now().hour
    if hour < 12:
        return "Good morning"
    elif hour < 17:
        return "Good afternoon"
    else:
        return "Good evening"


def build_briefing(facts: list[str]) -> str:
    """
    Assemble a briefing intro. The actual calendar/email/weather
    content is fetched in main.py and appended.
    """
    name = ""
    for f in facts:
        if "name" in f.lower():
            # extract the name part after "is"
            parts = f.lower().split("is")
            if len(parts) > 1:
                name = parts[1].strip().capitalize()
                break

    greeting = _greeting()
    if name:
        return f"{greeting}, {name}."
    return f"{greeting}."
