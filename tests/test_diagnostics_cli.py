from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import diagnostics


class _NonTtyInput:
    def isatty(self) -> bool:
        return False


class _TtyInput:
    def isatty(self) -> bool:
        return True


def test_resolve_repo_root_uses_diagnostics_location() -> None:
    assert diagnostics.resolve_repo_root() == Path(diagnostics.__file__).resolve().parent


def test_blocked_tasks_support_current_queue_schema(tmp_path: Path) -> None:
    queue = [
        {
            "task": "Repair diagnostics CLI",
            "session_name": "codex-diagnostics",
            "status": "blocked",
            "blocked_reason": "Waiting for focused repair",
        },
        {
            "task": "finished-task",
            "session_name": "Finished task",
            "status": "done",
        },
    ]
    (tmp_path / "WORK_QUEUE.json").write_text(json.dumps(queue), encoding="utf-8")
    output: list[str] = []

    diagnostics.print_blocked_tasks(tmp_path, output=output.append)

    rendered = "\n".join(output)
    assert "Total tasks: 2   Blocked: 1" in rendered
    assert "[codex-diagnostics] Repair diagnostics CLI" in rendered
    assert "Reason: Waiting for focused repair" in rendered
    assert "finished-task" not in rendered


def test_blocked_tasks_strip_terminal_control_sequences(tmp_path: Path) -> None:
    queue = [
        {
            "task": "Repair\x1b]0;spoofed-title\x07 diagnostics",
            "session_name": "safe-id\x1b[2J",
            "status": "blocked",
            "blocked_reason": "wait\x1b[31m now",
        }
    ]
    (tmp_path / "WORK_QUEUE.json").write_text(json.dumps(queue), encoding="utf-8")
    output: list[str] = []

    diagnostics.print_blocked_tasks(tmp_path, output=output.append)

    rendered = "\n".join(output)
    assert "\x1b" not in rendered
    assert "safe-id[2J" in rendered


def test_worldview_config_never_prints_secret_values(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    secret = "finnhub-secret-value"
    (repo / ".env").write_text(
        f"FINNHUB_API_KEY={secret}\n",
        encoding="utf-8",
    )
    output: list[str] = []

    diagnostics.print_worldview_config(
        repo,
        home=home,
        environ={"FINNHUB_API_KEY": secret},
        output=output.append,
    )

    rendered = "\n".join(output)
    assert "FINNHUB found in:" in rendered
    assert "FINNHUB_API_KEY in environment: SET" in rendered
    assert secret not in rendered


def test_pause_is_skipped_for_non_tty_input() -> None:
    with patch("builtins.input", side_effect=AssertionError("input must not run")):
        diagnostics.pause_if_requested(True, stdin=_NonTtyInput())


def test_pause_reads_input_when_requested_on_tty() -> None:
    with patch("builtins.input") as read_input:
        diagnostics.pause_if_requested(True, stdin=_TtyInput())

    read_input.assert_called_once_with("\nPress Enter to close...")


def test_main_does_not_pause_by_default(tmp_path: Path) -> None:
    with (
        patch.object(diagnostics, "resolve_repo_root", return_value=tmp_path),
        patch.object(diagnostics, "run_diagnostics") as run_diagnostics,
        patch("builtins.input", side_effect=AssertionError("input must not run")),
    ):
        result = diagnostics.main([])

    assert result == 0
    run_diagnostics.assert_called_once_with(tmp_path)
