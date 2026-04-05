import json
import os
from datetime import datetime

NOTES_FILE = os.path.join(os.path.dirname(__file__), "notes.json")


def _load() -> list:
    if not os.path.exists(NOTES_FILE):
        return []
    with open(NOTES_FILE) as f:
        return json.load(f)


def _save(notes: list) -> None:
    with open(NOTES_FILE, "w") as f:
        json.dump(notes, f, indent=2)


def add_note(content: str) -> str:
    notes = _load()
    notes.append({"id": len(notes) + 1, "date": str(datetime.now().strftime("%Y-%m-%d %H:%M")), "content": content})
    _save(notes)
    return f"Note saved."


def get_notes(n: int = 5) -> str:
    notes = _load()
    if not notes:
        return "You have no notes saved."
    recent = notes[-n:]
    lines = [f"{note['date']}: {note['content']}" for note in recent]
    return "Here are your recent notes: " + ". ".join(lines)


def search_notes(keyword: str) -> str:
    notes = _load()
    matches = [n for n in notes if keyword.lower() in n["content"].lower()]
    if not matches:
        return f"No notes found containing '{keyword}'."
    lines = [f"{n['date']}: {n['content']}" for n in matches[-5:]]
    return f"Found {len(matches)} notes: " + ". ".join(lines)
