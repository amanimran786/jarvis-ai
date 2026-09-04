from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis_v2.agent import AgentLimits, LocalAgentLoop
from jarvis_v2.config import LocalConfigurationError, LocalModelConfig
from jarvis_v2.model import ModelTurn
from jarvis_v2.tools import LocalToolError, ReadOnlyLocalTools


class FakeModel:
    def __init__(self, turns: list[ModelTurn]) -> None:
        self.turns = list(turns)

    def complete(self, messages, tools):
        assert messages
        assert {tool["function"]["name"] for tool in tools} == {"file", "git"}
        return self.turns.pop(0)


def tool_turn(name: str, arguments: dict, *, call_id: str = "call-1") -> ModelTurn:
    return ModelTurn(
        content="",
        tool_calls=(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            },
        ),
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8080/v1",
        "http://localhost:8080/v1",
        "http://192.168.1.9:8080/v1",
        "https://api.openai.com/v1",
        "http://user:password@127.0.0.1:8080/v1",
    ],
)
def test_local_model_config_rejects_any_non_loopback_or_credential_url(url):
    with pytest.raises(LocalConfigurationError):
        LocalModelConfig(base_url=url)


def test_agent_executes_one_validated_tool_then_finishes(tmp_path: Path):
    observed: list[tuple[str, dict]] = []

    def execute(name, arguments):
        observed.append((name, arguments))
        return "tracked files are clean"

    loop = LocalAgentLoop(
        model=FakeModel(
            [
                tool_turn("git", {"action": "status"}),
                ModelTurn(content="Repository is clean.", tool_calls=()),
            ]
        ),
        execute_tool=execute,
        state_dir=tmp_path,
    )

    result = loop.run("Inspect this repository")

    assert result.status == "completed"
    assert result.answer == "Repository is clean."
    assert result.steps == 2
    assert observed == [("git", {"action": "status"})]
    assert result.checkpoint_path.is_file()
    events = result.event_log_path.read_text(encoding="utf-8").splitlines()
    assert len(events) == 3
    assert [json.loads(line)["sequence"] for line in events] == [1, 2, 3]


def test_agent_blocks_after_repeated_invalid_calls(tmp_path: Path):
    malformed = ModelTurn(content="", tool_calls=({"id": "bad"},))
    loop = LocalAgentLoop(
        model=FakeModel([malformed, malformed]),
        execute_tool=lambda name, arguments: "unused",
        state_dir=tmp_path,
        limits=AgentLimits(max_consecutive_errors=2),
    )

    result = loop.run("Inspect")

    assert result.status == "blocked"
    assert "malformed tool call" in result.reason


def test_agent_can_resume_a_blocked_checkpoint(tmp_path: Path):
    first = LocalAgentLoop(
        model=FakeModel([ModelTurn(content="", tool_calls=())]),
        execute_tool=lambda name, arguments: "unused",
        state_dir=tmp_path,
        limits=AgentLimits(max_consecutive_errors=1),
    ).run("Inspect")
    second = LocalAgentLoop(
        model=FakeModel([ModelTurn(content="Recovered.", tool_calls=())]),
        execute_tool=lambda name, arguments: "unused",
        state_dir=tmp_path,
    ).run(resume_run_id=first.run_id)

    assert first.status == "blocked"
    assert second.status == "completed"
    assert second.answer == "Recovered."


def test_read_only_tools_reject_path_escape(tmp_path: Path):
    tools = ReadOnlyLocalTools(tmp_path)

    with pytest.raises(LocalToolError, match="escapes"):
        tools("file", {"action": "read", "path": "../outside.txt"})


def test_read_only_tools_reject_write_and_git_commit(tmp_path: Path):
    tools = ReadOnlyLocalTools(tmp_path)

    with pytest.raises(LocalToolError, match="file reads only"):
        tools("file", {"action": "write", "path": "x", "content": "x"})
    with pytest.raises(LocalToolError, match="read-only git"):
        tools("git", {"action": "commit", "message": "no"})


def test_agent_honors_owner_cancellation_and_checkpoints(tmp_path: Path):
    loop = LocalAgentLoop(
        model=FakeModel([]),
        execute_tool=lambda name, arguments: "unused",
        state_dir=tmp_path,
        is_cancelled=lambda: True,
    )

    result = loop.run("Inspect")

    assert result.status == "cancelled"
    assert result.reason == "cancelled by owner"
    assert json.loads(result.checkpoint_path.read_text(encoding="utf-8"))["status"] == "cancelled"


def test_git_show_rejects_option_injection(tmp_path: Path):
    tools = ReadOnlyLocalTools(tmp_path)

    with pytest.raises(LocalToolError, match="cannot begin"):
        tools("git", {"action": "show", "ref": "--help"})
