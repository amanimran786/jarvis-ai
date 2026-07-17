import json

from rich.console import Console

import jarvis_cli
import semantic_memory


def _write_turns(path, count):
    path.write_text(
        "\n".join(
            json.dumps(
                {
                    "timestamp": f"2026-07-16T18:{index:02d}:00-07:00",
                    "role": "user" if index % 2 == 0 else "assistant",
                    "content": f"history-marker-{index:02d}",
                }
            )
            for index in range(count)
        )
        + "\n",
        encoding="utf-8",
    )


def _render_history(monkeypatch, path, args=""):
    monkeypatch.setattr(semantic_memory, "VERBATIM_LOG_PATH", path)
    console = Console(record=True, width=160, color_system=None)

    result = jarvis_cli._print_history(args, console=console)

    assert result == 0
    return console.export_text()


def test_history_command_defaults_to_last_ten_temporary_turns(
    tmp_path,
    monkeypatch,
):
    history_path = tmp_path / "verbatim.jsonl"
    _write_turns(history_path, 12)

    rendered = _render_history(monkeypatch, history_path)

    assert "Conversation History" in rendered
    assert "history-marker-02" in rendered
    assert "history-marker-11" in rendered
    assert "history-marker-01" not in rendered


def test_history_command_accepts_explicit_turn_limit(tmp_path, monkeypatch):
    history_path = tmp_path / "verbatim.jsonl"
    _write_turns(history_path, 4)

    rendered = _render_history(monkeypatch, history_path, "2")

    assert "history-marker-02" in rendered
    assert "history-marker-03" in rendered
    assert "history-marker-01" not in rendered


def test_history_command_handles_missing_local_log(tmp_path, monkeypatch):
    rendered = _render_history(monkeypatch, tmp_path / "missing.jsonl")

    assert "No conversation history found." in rendered


def test_history_command_rejects_invalid_limit(capsys):
    result = jarvis_cli._print_history("unbounded")

    assert result == 1
    assert "positive integer" in capsys.readouterr().err


def test_history_command_rejects_limit_above_output_cap(capsys):
    result = jarvis_cli._print_history("101")

    assert result == 1
    assert "at most 100" in capsys.readouterr().err
