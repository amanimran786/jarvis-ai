import json
import os
import re
from datetime import datetime, timezone

import vault

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


def _trim_focus_text(text: str, limit: int = 160) -> str:
    cleaned = (text or "").replace("\n", " ")
    cleaned = re.sub(r"^\s*[-*]\s+", "", cleaned)
    cleaned = re.sub(r"\s[-*]\s+", ", ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,")
    cleaned = re.sub(r"(,\s*){2,}", ", ", cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "..."
    return cleaned


def _is_useful_focus_excerpt(text: str) -> bool:
    cleaned = _trim_focus_text(text, limit=240).lower()
    if not cleaned:
        return False
    boring_prefixes = (
        "purpose:",
        "this note is",
        "linked notes:",
    )
    return not any(cleaned.startswith(prefix) for prefix in boring_prefixes)


def _focus_line() -> str:
    for query in ("my priorities", "jarvis roadmap", "what are we building"):
        try:
            results = vault.search(query, topn=2)
        except Exception:
            return ""
        fallback_excerpt = ""
        for item in results:
            path = str(item.get("path", "")).replace("\\", "/").lower()
            if not path.startswith("wiki/brain/"):
                continue
            excerpt = _trim_focus_text(item.get("excerpt", ""))
            if not excerpt:
                continue
            if _is_useful_focus_excerpt(excerpt):
                return f"Current focus: {excerpt}."
            if not fallback_excerpt:
                fallback_excerpt = excerpt
        if fallback_excerpt:
            return f"Current focus: {fallback_excerpt}."
    return ""


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
    focus = _focus_line()
    if name:
        base = f"{greeting}, {name}."
    else:
        base = f"{greeting}."
    if focus:
        return f"{base} {focus}"
    return base
