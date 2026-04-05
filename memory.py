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
    "last_updated": None
}

_lock = threading.Lock()


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


def forget(keyword: str) -> bool:
    data = load()
    original = data["facts"]
    data["facts"] = [f for f in original if keyword.lower() not in f.lower()]
    if len(data["facts"]) < len(original):
        save(data)
        return True
    return False


def list_facts() -> list[str]:
    return load().get("facts", [])


# ── Preferences ───────────────────────────────────────────────────────────────

def set_preference(key: str, value: str) -> None:
    data = load()
    data["preferences"][key] = value
    save(data)


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


# ── Full context for system prompt ────────────────────────────────────────────

def get_context() -> str:
    data = load()
    parts = []

    if data["facts"]:
        facts = "\n".join(f"- {f}" for f in data["facts"])
        parts.append(f"Facts about the user:\n{facts}")

    if data["preferences"]:
        prefs = "\n".join(f"- {k}: {v}" for k, v in data["preferences"].items())
        parts.append(f"User preferences:\n{prefs}")

    if data["projects"]:
        projs = "\n".join(f"- {p['name']}" + (f" at {p['path']}" if p.get('path') else "") +
                          (f": {p['description']}" if p.get('description') else "")
                          for p in data["projects"])
        parts.append(f"User's projects:\n{projs}")

    if data["conversation_history"]:
        recent = data["conversation_history"][-3:]
        convos = "\n".join(f"- [{c['date']}] {c['summary']}" for c in recent)
        parts.append(f"Recent conversation summaries:\n{convos}")

    top = get_top_topics()
    if top:
        parts.append(f"Topics the user asks about most: {', '.join(top)}")

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
