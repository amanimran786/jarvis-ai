from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from rich.console import Console

import jarvis_cli
from harness.task_contract import (
    Capability,
    InputSpec,
    OutputSpec,
    SideEffect,
    TaskContract,
    TaskType,
)


def _contract(*, task_id: str = "CODEX-9", requires_approval: bool = False) -> TaskContract:
    return TaskContract(
        task_id=task_id,
        task_type=TaskType.CODE,
        description="Expose task contracts in the CLI",
        inputs=[InputSpec(name="task_id", type="str", description="Contract identifier")],
        outputs=[OutputSpec(name="table", type="str", description="Rendered Rich table")],
        side_effects=[SideEffect.WRITES_FILES],
        requires_capabilities=[Capability.FILESYSTEM, Capability.PYTHON],
        requires_approval=requires_approval,
        entry_point="jarvis_cli._print_contracts",
        preconditions=["TASK_CONTRACTS.json exists"],
        postconditions=["contract details are displayed"],
    )


def _console() -> tuple[Console, StringIO]:
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=160)
    return console, output


def test_contracts_list_shows_summary_and_validation_status() -> None:
    console, output = _console()
    contracts = {
        "CODEX-9": _contract(),
        "CODEX-bad": _contract(task_id="CODEX-bad", requires_approval=True),
    }

    with (
        patch.object(jarvis_cli, "load_contracts", return_value=contracts),
        patch.object(
            jarvis_cli,
            "validate_contract",
            side_effect=[(True, []), (False, ["first", "second"])],
        ),
    ):
        result = jarvis_cli._print_contracts("", console=console)

    rendered = output.getvalue()
    assert result == 0
    assert "Task Contracts" in rendered
    assert "CODEX-9" in rendered
    assert "code" in rendered
    assert "VALID" in rendered
    assert "CODEX-bad" in rendered
    assert "YES" in rendered
    assert "2 error(s)" in rendered


def test_contracts_detail_shows_full_contract_fields() -> None:
    console, output = _console()

    with patch.object(jarvis_cli, "load_contracts", return_value={"CODEX-9": _contract()}):
        result = jarvis_cli._print_contracts("CODEX-9", console=console)

    rendered = output.getvalue()
    assert result == 0
    assert "Contract: CODEX-9" in rendered
    assert "inputs" in rendered
    assert "outputs" in rendered
    assert "side_effects" in rendered
    assert "requires_capabilities" in rendered
    assert "preconditions" in rendered
    assert "postconditions" in rendered
    assert "filesystem" in rendered


def test_contracts_validate_reports_every_error_and_fails() -> None:
    console, output = _console()
    contracts = {"CODEX-bad": _contract(task_id="CODEX-bad")}

    with (
        patch.object(jarvis_cli, "load_contracts", return_value=contracts),
        patch.object(
            jarvis_cli,
            "validate_contract",
            return_value=(False, ["missing output", "missing precondition"]),
        ),
    ):
        result = jarvis_cli._print_contracts("validate", console=console)

    rendered = output.getvalue()
    assert result == 1
    assert "Contract Validation" in rendered
    assert "missing output" in rendered
    assert "missing precondition" in rendered
    assert "1 contract(s), 2 error(s)" in rendered


def test_contracts_unknown_task_id_returns_not_found() -> None:
    console, output = _console()

    with patch.object(jarvis_cli, "load_contracts", return_value={}):
        result = jarvis_cli._print_contracts("missing-task", console=console)

    assert result == 1
    assert "No contract found for task_id: missing-task" in output.getvalue()


def test_contracts_command_dispatches_locally() -> None:
    with patch.object(jarvis_cli, "_print_contracts", return_value=0) as print_contracts:
        result = jarvis_cli._handle_console_command("/contracts validate")

    assert result == 0
    print_contracts.assert_called_once_with("validate")
