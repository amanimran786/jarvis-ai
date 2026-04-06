import json
import os
import tempfile
import threading
from datetime import date, datetime

MEMORY_FILE = os.path.join(os.path.dirname(__file__), "memory.json")

_DEFAULTS = {
    "facts": [],
    "preferences": {},
    "conversation_history": [],
    "projects": [],
    "topic_counts": {},
    "working_memory": {},
    "long_term_profile": {},
    "last_updated": None
}

_lock = threading.Lock()


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    ordered = []
    for item in items:
        cleaned = (item or "").strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(cleaned)
    return ordered


def _trim(text: str, limit: int = 180) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _conversation_focus(conversations: list[dict], limit: int = 3) -> list[str]:
    items = []
    for convo in conversations[-limit:]:
        summary = (convo.get("summary") or "").strip()
        if summary:
            items.append(_trim(summary, 160))
    return items


def _build_working_memory(data: dict) -> dict:
    projects = data.get("projects", [])
    prefs = data.get("preferences", {})
    top_topics = get_top_topics(5)
    recent_focus = _conversation_focus(data.get("conversation_history", []), limit=3)

    active_projects = []
    for project in projects[:3]:
        name = (project.get("name") or "").strip()
        desc = (project.get("description") or "").strip()
        if not name:
            continue
        active_projects.append(_trim(f"{name}: {desc}" if desc else name, 140))

    assist_prefs = []
    for key, value in list(prefs.items())[:5]:
        assist_prefs.append(_trim(f"{key}: {value}", 100))

    return {
        "active_projects": _dedupe_keep_order(active_projects),
        "recent_focus": _dedupe_keep_order(recent_focus),
        "recurring_topics": _dedupe_keep_order(top_topics),
        "assist_preferences": _dedupe_keep_order(assist_prefs),
        "updated_at": str(datetime.now().strftime("%Y-%m-%d %H:%M")),
    }


def _build_long_term_profile(data: dict) -> dict:
    facts = _dedupe_keep_order(data.get("facts", []))
    prefs = data.get("preferences", {})
    projects = data.get("projects", [])
    top_topics = _dedupe_keep_order(get_top_topics(5))

    profile_parts = []
    if facts:
        profile_parts.append("Known facts: " + "; ".join(_trim(fact, 80) for fact in facts[:5]))
    if projects:
        project_names = ", ".join(project["name"] for project in projects[:3] if project.get("name"))
        if project_names:
            profile_parts.append(f"Active projects: {project_names}")
    if prefs:
        pref_keys = ", ".join(list(prefs.keys())[:4])
        if pref_keys:
            profile_parts.append(f"Preferences Jarvis should honor: {pref_keys}")
    if top_topics:
        profile_parts.append(f"Recurring topics: {', '.join(top_topics[:5])}")

    return {
        "summary": _trim(". ".join(profile_parts), 420),
        "stable_facts": facts[:8],
        "project_names": [project["name"] for project in projects if project.get("name")][:5],
        "recurring_topics": top_topics[:5],
        "updated_at": str(datetime.now().strftime("%Y-%m-%d %H:%M")),
    }


def load() -> dict:
    with _lock:
        if not os.path.exists(MEMORY_FILE):
            return dict(_DEFAULTS)
        try:
            with open(MEMORY_FILE) as f:
                content = f.read().strip()
            if not content:
                return dict(_DEFAULTS)
            data = json.loads(content)
        except (json.JSONDecodeError, OSError):
            # Corrupted file — return defaults, don't crash
            return dict(_DEFAULTS)
        for k, v in _DEFAULTS.items():
            data.setdefault(k, v)
        return data


def save(data: dict) -> None:
    with _lock:
        data["last_updated"] = str(date.today())
        # Write to a unique temp file first, then rename — prevents partial
        # writes and avoids cross-thread collisions on a shared .tmp name.
        directory = os.path.dirname(MEMORY_FILE) or "."
        fd, tmp = tempfile.mkstemp(prefix="memory.", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, MEMORY_FILE)


# ── Facts ─────────────────────────────────────────────────────────────────────

def add_fact(fact: str) -> None:
    data = load()
    if fact not in data["facts"]:
        data["facts"].append(fact)
        save(data)
        consolidate_memory()


def forget(keyword: str) -> bool:
    data = load()
    original = data["facts"]
    data["facts"] = [f for f in original if keyword.lower() not in f.lower()]
    if len(data["facts"]) < len(original):
        save(data)
        consolidate_memory()
        return True
    return False


def list_facts() -> list[str]:
    return load().get("facts", [])


# ── Preferences ───────────────────────────────────────────────────────────────

def set_preference(key: str, value: str) -> None:
    data = load()
    data["preferences"][key] = value
    save(data)
    consolidate_memory()


def get_preference(key: str, default=None):
    return load()["preferences"].get(key, default)


def get_all_preferences() -> dict:
    return load().get("preferences", {})


# ── Conversation history ───────────────────────────────────────────────────────

def save_conversation(summary: str) -> None:
    """Save a summary of a completed conversation session."""
    data = load()
    data["conversation_history"].append({
        "date": str(datetime.now().strftime("%Y-%m-%d %H:%M")),
        "summary": summary
    })
    # Keep last 30 conversations
    data["conversation_history"] = data["conversation_history"][-30:]
    save(data)
    consolidate_memory()


def get_recent_conversations(n: int = 5) -> list[dict]:
    return load()["conversation_history"][-n:]


# ── Project tracking ──────────────────────────────────────────────────────────

def add_project(name: str, path: str = "", description: str = "") -> None:
    data = load()
    existing = [p for p in data["projects"] if p["name"] == name]
    if existing:
        existing[0].update({"path": path, "description": description})
    else:
        data["projects"].append({"name": name, "path": path, "description": description})
    save(data)
    consolidate_memory()


def get_projects() -> list[dict]:
    return load().get("projects", [])


# ── Topic learning ────────────────────────────────────────────────────────────

def track_topic(user_input: str) -> None:
    """Track what topics the user asks about most."""
    keywords = ["code", "python", "javascript", "email", "calendar", "music",
                "weather", "news", "file", "terminal", "search", "timer"]
    data = load()
    for kw in keywords:
        if kw in user_input.lower():
            data["topic_counts"][kw] = data["topic_counts"].get(kw, 0) + 1
    save(data)


def get_top_topics(n: int = 3) -> list[str]:
    counts = load().get("topic_counts", {})
    return sorted(counts, key=counts.get, reverse=True)[:n]


def consolidate_memory() -> dict:
    data = load()
    data["working_memory"] = _build_working_memory(data)
    data["long_term_profile"] = _build_long_term_profile(data)
    save(data)
    return {
        "ok": True,
        "working_memory": data["working_memory"],
        "long_term_profile": data["long_term_profile"],
    }


def memory_status() -> dict:
    data = load()
    working = data.get("working_memory", {})
    durable = data.get("long_term_profile", {})
    return {
        "facts": len(data.get("facts", [])),
        "preferences": len(data.get("preferences", {})),
        "projects": len(data.get("projects", [])),
        "conversation_summaries": len(data.get("conversation_history", [])),
        "top_topics": get_top_topics(5),
        "working_memory_ready": bool(working),
        "long_term_profile_ready": bool(durable.get("summary")),
        "working_memory": working,
        "long_term_profile": durable,
    }


# ── Full context for system prompt ────────────────────────────────────────────

def get_context() -> str:
    data = load()
    parts = []

    if not data.get("working_memory") or not data.get("long_term_profile"):
        data.update(consolidate_memory())
        data = load()

    durable = data.get("long_term_profile", {})
    if durable.get("summary"):
        parts.append(f"Durable user profile:\n{durable['summary']}")

    working = data.get("working_memory", {})
    working_lines = []
    if working.get("active_projects"):
        working_lines.append("Active projects: " + "; ".join(working["active_projects"]))
    if working.get("recent_focus"):
        working_lines.append("Recent focus: " + "; ".join(working["recent_focus"]))
    if working.get("recurring_topics"):
        working_lines.append("Recurring topics: " + ", ".join(working["recurring_topics"]))
    if working.get("assist_preferences"):
        working_lines.append("Assist preferences: " + "; ".join(working["assist_preferences"]))
    if working_lines:
        parts.append("Working memory:\n" + "\n".join(f"- {line}" for line in working_lines))

    if data["conversation_history"]:
        recent = data["conversation_history"][-3:]
        convos = "\n".join(f"- [{c['date']}] {c['summary']}" for c in recent)
        parts.append(f"Short-term session summaries:\n{convos}")

    # Inject learning context from learner module
    try:
        from learner import get_learning_context
        learning = get_learning_context()
        if learning:
            parts.append(learning.strip())
    except Exception:
        pass

    if not parts:
        return ""
    return "\n\n" + "\n\n".join(parts)
