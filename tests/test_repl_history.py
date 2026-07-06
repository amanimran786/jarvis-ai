import json

import pytest
from rich.console import Console
from rich.table import Table

from harness.repl_history import (
    DEFAULT_HISTORY_LIMIT,
    HistoryTurn,
    build_history_table,
    extract_history_turn,
    extract_history_turns,
    history_table,
    load_history,
    parse_history_limit,
    truncate_content,
)


def _write_jsonl(path, records):
    path.write_text(
        "\n".join(
            record if isinstance(record, str) else json.dumps(record)
            for record in records
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("value", [None, "", "   "])
def test_parse_history_limit_defaults_to_ten(value):
    assert parse_history_limit(value) == DEFAULT_HISTORY_LIMIT


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, 1), (25, 25), ("20", 20), (" 7 ", 7), ("+2", 2), ("02", 2)],
)
def test_parse_history_limit_accepts_positive_integers(value, expected):
    assert parse_history_limit(value) == expected


@pytest.mark.parametrize("value", [0, -1, "0", "-2", "1.5", "abc", 1.5, True])
def test_parse_history_limit_rejects_non_positive_or_invalid_values(value):
    with pytest.raises(ValueError, match="positive integer"):
        parse_history_limit(value)


def test_truncate_content_normalizes_whitespace_and_caps_at_120_characters():
    content = ("word \n\t" * 40).strip()

    result = truncate_content(content)

    assert len(result) == 120
    assert result.endswith("...")
    assert "\n" not in result


def test_extract_history_turn_supports_nested_message_and_role_aliases():
    record = {
        "timestamp": "2026-07-05T12:00:00Z",
        "message": {"role": "human", "content": "Hello"},
    }

    turn = extract_history_turn(record)

    assert turn == HistoryTurn("2026-07-05T12:00:00Z", "user", "Hello")


def test_extract_history_turns_supports_persisted_conversation_pairs():
    record = {
        "timestamp": "2026-07-05T12:00:00Z",
        "user": "What changed?",
        "assistant": "The local route is healthy.",
        "model": "local-model",
    }

    turns = extract_history_turns(record)

    assert turns == [
        HistoryTurn("2026-07-05T12:00:00Z", "user", "What changed?"),
        HistoryTurn(
            "2026-07-05T12:00:00Z",
            "assistant",
            "The local route is healthy.",
        ),
    ]


def test_load_history_counts_each_persisted_pair_as_two_turns(tmp_path):
    path = tmp_path / "verbatim.jsonl"
    _write_jsonl(
        path,
        [
            {"timestamp": "1", "user": "u1", "assistant": "a1"},
            {"timestamp": "2", "user": "u2", "assistant": "a2"},
        ],
    )

    turns = load_history(path, limit=3)

    assert [(turn.role, turn.content) for turn in turns] == [
        ("assistant", "a1"),
        ("user", "u2"),
        ("assistant", "a2"),
    ]


def test_load_history_returns_last_ten_valid_turns_defensively(tmp_path):
    path = tmp_path / "usage_log.jsonl"
    valid_turns = [
        {"timestamp": f"2026-07-05T12:{index:02d}:00Z", "role": "user", "content": f"turn {index}"}
        for index in range(12)
    ]
    records = [
        "not json",
        {"provider": "ollama", "metadata": {"api_key": "do-not-render"}},
        {"timestamp": "ignored", "role": "system", "content": "system prompt"},
        ["not", "an", "object"],
        *valid_turns,
    ]
    _write_jsonl(path, records)

    turns = load_history(path)

    assert len(turns) == 10
    assert turns[0].content == "turn 2"
    assert turns[-1].content == "turn 11"
    assert all(turn.role == "user" for turn in turns)


def test_load_history_honors_optional_positive_limit(tmp_path):
    path = tmp_path / "usage_log.jsonl"
    _write_jsonl(
        path,
        [
            {"timestamp": str(index), "role": "assistant", "content": str(index)}
            for index in range(5)
        ],
    )

    turns = load_history(path, limit="2")

    assert [turn.content for turn in turns] == ["3", "4"]


def test_load_history_returns_empty_for_missing_or_unreadable_path(tmp_path):
    assert load_history(tmp_path / "missing.jsonl") == []
    assert load_history(tmp_path) == []


def test_build_history_table_applies_role_and_timestamp_styles():
    turns = [
        HistoryTurn("2026-07-05T12:00:00Z", "user", "hello"),
        HistoryTurn("2026-07-05T12:01:00Z", "assistant", "hi"),
    ]

    table = build_history_table(turns)

    assert isinstance(table, Table)
    assert str(table.columns[0]._cells[0].style) == "dim"
    assert str(table.columns[1]._cells[0].style) == "bold cyan"
    assert str(table.columns[1]._cells[1].style) == "bold green"


def test_history_table_does_not_render_unrecognized_metadata(tmp_path):
    path = tmp_path / "usage_log.jsonl"
    _write_jsonl(
        path,
        [
            {
                "timestamp": "2026-07-05T12:00:00Z",
                "role": "user",
                "content": "safe visible content",
                "metadata": {"api_key": "hidden-metadata-value"},
            }
        ],
    )
    console = Console(record=True, width=160)

    console.print(history_table(path))
    rendered = console.export_text()

    assert "safe visible content" in rendered
    assert "hidden-metadata-value" not in rendered
