"""Conversation-history parsing and Rich rendering for the Jarvis REPL."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from rich import box
from rich.table import Table
from rich.text import Text


DEFAULT_HISTORY_LIMIT = 10
MAX_CONTENT_CHARS = 120

_ROLE_ALIASES = {
    "assistant": "assistant",
    "human": "user",
    "jarvis": "assistant",
    "model": "assistant",
    "user": "user",
}
_ROLE_STYLES = {
    "user": "bold cyan",
    "assistant": "bold green",
}


@dataclass(frozen=True)
class HistoryTurn:
    """A single display-safe conversation turn."""

    timestamp: str
    role: str
    content: str


def parse_history_limit(value: str | int | None = None) -> int:
    """Parse an optional positive history limit, defaulting to ten turns."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_HISTORY_LIMIT
    if isinstance(value, bool):
        raise ValueError("history limit must be a positive integer")
    if not isinstance(value, (str, int)):
        raise ValueError("history limit must be a positive integer")
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("history limit must be a positive integer") from exc
    if limit <= 0:
        raise ValueError("history limit must be a positive integer")
    return limit


def truncate_content(content: str, max_chars: int = MAX_CONTENT_CHARS) -> str:
    """Collapse display whitespace and truncate content to ``max_chars``."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    normalized = " ".join(content.split())
    if len(normalized) <= max_chars:
        return normalized
    if max_chars <= 3:
        return "." * max_chars
    return normalized[: max_chars - 3].rstrip() + "..."


def extract_history_turn(record: Mapping[str, Any]) -> HistoryTurn | None:
    """Extract a user or assistant turn from a supported JSONL record shape."""
    payload: Mapping[str, Any] = record
    nested_message = record.get("message")
    if isinstance(nested_message, Mapping):
        payload = nested_message

    raw_role = payload.get("role")
    raw_content = payload.get("content")
    if not isinstance(raw_role, str) or not isinstance(raw_content, str):
        return None

    role = _ROLE_ALIASES.get(raw_role.strip().lower())
    if role is None:
        return None

    timestamp = payload.get("timestamp")
    if not isinstance(timestamp, str):
        timestamp = record.get("timestamp")
    if not isinstance(timestamp, str):
        timestamp = record.get("ts") or record.get("created_at")
    if not isinstance(timestamp, str):
        timestamp = ""

    return HistoryTurn(
        timestamp=timestamp.strip(),
        role=role,
        content=truncate_content(raw_content),
    )


def extract_history_turns(record: Mapping[str, Any]) -> list[HistoryTurn]:
    """Extract one role/content turn or a persisted user/assistant turn pair."""
    single_turn = extract_history_turn(record)
    if single_turn is not None:
        return [single_turn]

    timestamp = record.get("timestamp")
    rendered_timestamp = timestamp.strip() if isinstance(timestamp, str) else ""
    turns: list[HistoryTurn] = []
    for role, field in (("user", "user"), ("assistant", "assistant")):
        content = record.get(field)
        if isinstance(content, str) and content.strip():
            turns.append(
                HistoryTurn(
                    timestamp=rendered_timestamp,
                    role=role,
                    content=truncate_content(content),
                )
            )
    return turns


def _default_history_log_path() -> Path:
    import semantic_memory

    return Path(semantic_memory.VERBATIM_LOG_PATH)


def load_history(
    path: str | Path | None = None,
    limit: str | int | None = None,
) -> list[HistoryTurn]:
    """Read the last requested conversation turns from Jarvis's local JSONL log."""
    parsed_limit = parse_history_limit(limit)
    turns: deque[HistoryTurn] = deque(maxlen=parsed_limit)
    log_path = Path(path) if path is not None else _default_history_log_path()

    try:
        with log_path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(record, Mapping):
                    continue
                turns.extend(extract_history_turns(record))
    except OSError:
        return []

    return list(turns)


def build_history_table(turns: list[HistoryTurn]) -> Table:
    """Build a Rich table without printing it, for REPL-owned rendering."""
    table = Table(title="Conversation History", box=box.SIMPLE_HEAVY, expand=True)
    table.add_column("Timestamp", style="dim", no_wrap=True)
    table.add_column("Role", no_wrap=True)
    table.add_column("Content", overflow="fold")

    for turn in turns:
        table.add_row(
            Text(turn.timestamp or "-", style="dim"),
            Text(turn.role, style=_ROLE_STYLES[turn.role]),
            Text(turn.content),
        )

    if not turns:
        table.caption = "No conversation history found."
    return table


def history_table(
    path: str | Path | None = None,
    limit: str | int | None = None,
) -> Table:
    """Load local conversation history and return its Rich table."""
    return build_history_table(load_history(path=path, limit=limit))
