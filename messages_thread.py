"""
Lightweight iMessage conversation thread tracker.

Since chat.db requires Full Disk Access (which Jarvis doesn't have), this module
maintains a Jarvis-side record of conversations: messages Jarvis sends and incoming
messages the user relays. Stored in Application Support so it persists across restarts.
"""

import json
import os
import threading
from datetime import datetime
from pathlib import Path

_DATA_DIR = Path(os.path.expanduser("~/Library/Application Support/Jarvis"))
_THREADS_FILE = _DATA_DIR / "message_threads.json"
_lock = threading.Lock()

_MAX_THREAD_MSGS = 30  # keep last N messages per contact


def _load() -> dict:
    try:
        if _THREADS_FILE.exists():
            return json.loads(_THREADS_FILE.read_text())
    except Exception:
        pass
    return {}


def _save(data: dict) -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _THREADS_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


def _normalize_key(contact: str) -> str:
    """Use phone number or lowercased name as thread key."""
    import re
    digits = re.sub(r"\D", "", contact)
    if len(digits) >= 7:
        return digits
    return contact.strip().lower()


def _thread_keys(contact: str, address: str = "") -> list[str]:
    """Return all stable keys that should point at one conversation."""
    keys: list[str] = []
    for value in (contact, address):
        normalized = _normalize_key(value or "")
        if normalized and normalized not in keys:
            keys.append(normalized)
    return keys


def _merge_threads(data: dict, keys: list[str], contact: str, address: str = "") -> tuple[str, dict]:
    """Merge name/address aliases into the first key so future lookups agree."""
    primary = keys[0]
    merged = {"contact": contact or primary, "address": address or "", "messages": []}
    seen: set[tuple[str, str, str]] = set()
    for key in keys:
        thread = data.get(key) or {}
        if thread.get("contact") and not merged.get("contact"):
            merged["contact"] = thread["contact"]
        if thread.get("address") and not merged.get("address"):
            merged["address"] = thread["address"]
        for msg in thread.get("messages", []):
            marker = (msg.get("direction", ""), msg.get("body", ""), msg.get("ts", ""))
            if marker not in seen:
                seen.add(marker)
                merged["messages"].append(msg)
    merged["messages"].sort(key=lambda m: m.get("ts", ""))
    merged["messages"] = merged["messages"][-_MAX_THREAD_MSGS:]
    data[primary] = merged
    for key in keys[1:]:
        data[key] = merged
    return primary, merged


def record_sent(contact: str, address: str, body: str) -> None:
    """Record a message Jarvis sent to a contact."""
    keys = _thread_keys(contact, address)
    if not keys:
        return
    with _lock:
        data = _load()
        _, thread = _merge_threads(data, keys, contact, address)
        thread["contact"] = contact
        thread["address"] = address or thread.get("address", "")
        thread["messages"].append({
            "direction": "out",
            "body": body,
            "ts": datetime.now().isoformat(),
        })
        thread["messages"] = thread["messages"][-_MAX_THREAD_MSGS:]
        _save(data)


def record_incoming(contact: str, body: str) -> None:
    """Record an incoming message relayed by the user."""
    keys = _thread_keys(contact)
    if not keys:
        return
    with _lock:
        data = _load()
        _, thread = _merge_threads(data, keys, contact, "")
        thread["contact"] = thread.get("contact") or contact
        thread["messages"].append({
            "direction": "in",
            "body": body,
            "ts": datetime.now().isoformat(),
        })
        thread["messages"] = thread["messages"][-_MAX_THREAD_MSGS:]
        _save(data)


def get_thread(contact: str, last_n: int = 10) -> list[dict]:
    """Return the last N messages for a contact, oldest first."""
    keys = _thread_keys(contact)
    with _lock:
        data = _load()
        thread = next((data.get(key) for key in keys if data.get(key)), {})
        return thread.get("messages", [])[-last_n:]


def format_thread_for_prompt(contact: str, last_n: int = 10, my_name: str = "Aman") -> str:
    """Format conversation history as a plain-text block for LLM context."""
    msgs = get_thread(contact, last_n)
    if not msgs:
        return ""
    lines = []
    for m in msgs:
        sender = my_name if m["direction"] == "out" else contact
        lines.append(f"{sender}: {m['body']}")
    return "\n".join(lines)


def list_threads() -> list[dict]:
    """Return all known threads with contact name, address, message count."""
    with _lock:
        data = _load()
        result = []
        seen_threads: set[tuple[str, str, int, str]] = set()
        for key, thread in data.items():
            msgs = thread.get("messages", [])
            last = msgs[-1] if msgs else {}
            marker = (
                thread.get("contact", key),
                thread.get("address", key),
                len(msgs),
                last.get("ts", ""),
            )
            if marker in seen_threads:
                continue
            seen_threads.add(marker)
            last = msgs[-1] if msgs else None
            result.append({
                "key": key,
                "contact": thread.get("contact", key),
                "address": thread.get("address", key),
                "message_count": len(msgs),
                "last_ts": last["ts"] if last else None,
                "last_direction": last["direction"] if last else None,
            })
        result.sort(key=lambda x: x["last_ts"] or "", reverse=True)
        return result
