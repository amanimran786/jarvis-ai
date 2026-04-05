"""
Shared conversation context manager for Jarvis.

This keeps only the active task in prompt history, compacts older turns into a
small carried summary, and rotates to a new session when the topic changes.
"""

from __future__ import annotations

import re
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta

import memory as mem


MAX_ACTIVE_TURNS = 4
SESSION_TIMEOUT_MINUTES = 20
TOPIC_OVERLAP_THRESHOLD = 0.12
MIN_ROTATE_MESSAGES = 2
RECENT_REQUEST_STATS = 50
FOLLOW_UP_PREFIXES = (
    "and ",
    "also ",
    "what about",
    "why ",
    "how ",
    "can you ",
    "could you ",
    "go on",
    "continue",
    "expand",
    "tell me more",
)

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "do", "for", "from",
    "how", "i", "if", "in", "is", "it", "me", "my", "of", "on", "or", "please",
    "that", "the", "this", "to", "we", "what", "when", "where", "why", "with", "you",
    "your", "jarvis",
}

_LOCK = threading.Lock()
_STATE = {
    "id": uuid.uuid4().hex[:8],
    "messages": [],
    "summary": "",
    "started_at": datetime.now(),
    "last_active": datetime.now(),
    "recent_user_topics": [],
    "rotations": 0,
}
_RECENT_STATS = deque(maxlen=RECENT_REQUEST_STATS)


def _tokenize(text: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(token) > 2 and token not in _STOPWORDS
    }


def _topic_overlap(a: str, b: str) -> float:
    left = _tokenize(a)
    right = _tokenize(b)
    if not left or not right:
        return 0.0
    return len(left & right) / max(len(left), len(right))


def _trim_text(text: str, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def summarize_transcript(lines: list[str] | tuple[str, ...]) -> str:
    cleaned = [re.sub(r"^\w+:\s*", "", line).strip() for line in lines if line and line.strip()]
    if not cleaned:
        return "Short Jarvis conversation."
    first = _trim_text(cleaned[0], 120)
    last = _trim_text(cleaned[-1], 120)
    if len(cleaned) == 1:
        return f"Conversation about {first}"
    return f"Conversation started with {first} and ended with {last}"


def _session_summary(messages: list[dict], existing_summary: str = "") -> str:
    lines = [f"{m['role'].title()}: {_trim_text(m['content'], 140)}" for m in messages[-8:]]
    summary = summarize_transcript(lines)
    if existing_summary:
        return _trim_text(f"{existing_summary} {summary}", 600)
    return _trim_text(summary, 600)


def _rotate_if_needed(user_input: str) -> None:
    if not _STATE["messages"]:
        return

    now = datetime.now()
    age = now - _STATE["last_active"]
    recent_topic_text = " ".join(_STATE["recent_user_topics"][-3:])
    overlap = _topic_overlap(user_input, recent_topic_text)

    should_rotate = False
    if age > timedelta(minutes=SESSION_TIMEOUT_MINUTES):
        should_rotate = True
    elif (
        len(_STATE["messages"]) >= MIN_ROTATE_MESSAGES
        and overlap < TOPIC_OVERLAP_THRESHOLD
        and not user_input.lower().strip().startswith(FOLLOW_UP_PREFIXES)
    ):
        should_rotate = True

    if not should_rotate:
        return

    summary = _session_summary(_STATE["messages"], _STATE["summary"])
    if summary:
        mem.save_conversation(summary)

    _STATE["id"] = uuid.uuid4().hex[:8]
    _STATE["messages"] = []
    _STATE["summary"] = ""
    _STATE["started_at"] = now
    _STATE["last_active"] = now
    _STATE["recent_user_topics"] = []
    _STATE["rotations"] += 1


def begin_turn(user_input: str) -> None:
    with _LOCK:
        _rotate_if_needed(user_input)
        _STATE["messages"].append({"role": "user", "content": user_input})
        _STATE["last_active"] = datetime.now()
        _STATE["recent_user_topics"].append(user_input)
        _STATE["recent_user_topics"] = _STATE["recent_user_topics"][-3:]


def end_turn(response: str) -> None:
    with _LOCK:
        _STATE["messages"].append({"role": "assistant", "content": response})
        _STATE["last_active"] = datetime.now()
        _compact_if_needed()


def _compact_if_needed() -> None:
    max_messages = MAX_ACTIVE_TURNS * 2
    if len(_STATE["messages"]) <= max_messages:
        return

    overflow = len(_STATE["messages"]) - max_messages
    compacted = _STATE["messages"][:overflow]
    _STATE["messages"] = _STATE["messages"][overflow:]
    _STATE["summary"] = _session_summary(compacted, _STATE["summary"])


def build_prompt_state(system_prompt: str, system_extra: str = "") -> tuple[str, list[dict], dict]:
    with _LOCK:
        system_parts = [system_prompt]
        if _STATE["summary"]:
            system_parts.append(
                "Active conversation carry-over summary:\n"
                + _STATE["summary"]
            )
        if system_extra:
            system_parts.append(system_extra)

        system = "\n\n".join(part for part in system_parts if part)
        messages = list(_STATE["messages"])
        stats = {
            "session_id": _STATE["id"],
            "active_messages": len(messages),
            "summary_chars": len(_STATE["summary"]),
            "message_chars": sum(len(m.get("content", "")) for m in messages),
            "estimated_prompt_tokens": max(1, (len(system) + sum(len(m.get("content", "")) for m in messages)) // 4),
            "rotations": _STATE["rotations"],
        }
        return system, messages, stats


def get_stats() -> dict:
    _, _, stats = build_prompt_state("")
    return stats


def record_request_stats(model: str, source: str = "chat") -> dict:
    stats = get_stats()
    entry = {
        "timestamp": datetime.now().isoformat(),
        "model": model,
        "source": source,
        **stats,
    }
    _RECENT_STATS.append(entry)
    return entry


def recent_request_stats(limit: int = 10) -> list[dict]:
    return list(_RECENT_STATS)[-limit:]
