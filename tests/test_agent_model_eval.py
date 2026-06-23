"""Deterministic tests for the non-mutating agent protocol evaluator."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from local_runtime import agent_model_eval


@pytest.fixture(autouse=True)
def _no_usage_writes():
    with patch.object(agent_model_eval.usage_tracker, "record"):
        yield


def _call(name, arguments):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))


def _response(*, calls=(), content="", thinking="", prompt=20, completion=5):
    return SimpleNamespace(
        message=SimpleNamespace(tool_calls=list(calls), content=content, thinking=thinking),
        prompt_eval_count=prompt,
        eval_count=completion,
    )


class FakeClient:
    def __init__(self, responses, digest="sha256:glm52"):
        self.responses = list(responses)
        self.calls = []
        self.digest = digest

    def list(self):
        return SimpleNamespace(models=[
            SimpleNamespace(model="glm-5.2:latest", digest=self.digest),
        ])

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def test_protocol_eval_passes_without_executing_tools():
    client = FakeClient([
        _response(calls=[_call("read_file", {"path": "config.py"})]),
        _response(content="The synthetic result confirms the configured default."),
        _response(calls=[
            _call("delegate_subtask", {"agent": "backend_engineer", "task": "implementation review"}),
            _call("delegate_subtask", {"agent": "security_reviewer", "task": "threat review"}),
        ], thinking="brief reasoning"),
        _response(content="1. Measure. 2. Compare. 3. Decide."),
    ])
    with patch.object(agent_model_eval.brain_ollama, "list_local_models", return_value=["glm-5.2:latest"]):
        result = agent_model_eval.run_eval(
            "glm-5.2", client=client, expected_digest="sha256:glm52",
        )

    assert result["ok"] is True
    assert result["protocol_ready"] is True
    assert result["promotion_ready"] is False
    assert result["tools_executed"] == 0
    assert result["pass_rate"] == 1.0
    assert result["thinking_chars"] == len("brief reasoning")
    assert result["nested_subagents_executed"] is False
    assert len(client.calls) == 4


def test_protocol_eval_rejects_missing_or_cloud_model_before_calls():
    client = FakeClient([])
    with patch.object(agent_model_eval.brain_ollama, "list_local_models", return_value=["glm-4.7-flash"]):
        missing = agent_model_eval.run_eval(
            "glm-5.2", client=client, expected_digest="sha256:glm52",
        )
        cloud = agent_model_eval.run_eval("glm-5.2:cloud", client=client)

    assert missing["ok"] is False
    assert cloud == {"ok": False, "promotion_ready": False, "error": "A non-cloud model is required."}
    assert client.calls == []


def test_protocol_eval_fails_hard_gate_on_hallucinated_tool():
    case = agent_model_eval.ProtocolCase(
        id="invalid_tool",
        prompt="Inspect config.",
        expected_tools=("read_file",),
    )
    client = FakeClient([_response(calls=[_call("shell_exec", {"command": "cat config.py"})])])
    with patch.object(agent_model_eval.brain_ollama, "list_local_models", return_value=["glm-5.2"]):
        result = agent_model_eval.run_eval(
            "glm-5.2", client=client, cases=[case], expected_digest="sha256:glm52",
        )

    assert result["ok"] is True
    assert result["promotion_ready"] is False
    assert result["schema_valid_rate"] == 0.0
    assert result["results"][0]["tool_names"] == ["shell_exec"]


def test_protocol_eval_rejects_semantically_wrong_tool_arguments():
    case = agent_model_eval.ProtocolCase(
        id="wrong_path",
        prompt="Inspect config.py.",
        expected_tools=("read_file",),
        expected_paths=("config.py",),
    )
    client = FakeClient([_response(calls=[_call("read_file", {"path": "README.md"})])])
    with patch.object(agent_model_eval.brain_ollama, "list_local_models", return_value=["glm-5.2"]):
        result = agent_model_eval.run_eval(
            "glm-5.2", client=client, cases=[case], expected_digest="sha256:glm52",
        )

    assert result["protocol_ready"] is False
    assert result["results"][0]["argument_semantics_match"] is False


def test_protocol_eval_sanitizes_provider_failure():
    class BrokenClient:
        def list(self):
            return SimpleNamespace(models=[
                SimpleNamespace(model="glm-5.2", digest="sha256:glm52"),
            ])

        def chat(self, **kwargs):
            raise RuntimeError("secret endpoint detail")

    with patch.object(agent_model_eval.brain_ollama, "list_local_models", return_value=["glm-5.2"]):
        result = agent_model_eval.run_eval(
            "glm-5.2", client=BrokenClient(), expected_digest="sha256:glm52",
        )

    assert result["ok"] is False
    assert result["promotion_ready"] is False
    assert result["error"] == "Protocol evaluation failed: RuntimeError"
    assert "secret" not in str(result)


def test_glm52_protocol_eval_requires_matching_digest():
    client = FakeClient([], digest="sha256:actual")
    with patch.object(agent_model_eval.brain_ollama, "list_local_models", return_value=["glm-5.2"]):
        missing = agent_model_eval.run_eval("glm-5.2", client=client, expected_digest="")
        mismatch = agent_model_eval.run_eval(
            "glm-5.2", client=client, expected_digest="sha256:expected",
        )

    assert "LOCAL_GLM52_DIGEST is required" in missing["error"]
    assert "does not match" in mismatch["error"]
    assert client.calls == []
