"""Native Ollama tool-loop context bounds, telemetry, and call safety."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from brains import brain_ollama


def _response(content="", calls=None, prompt_tokens=10, completion_tokens=4):
    tool_calls = [
        SimpleNamespace(function=SimpleNamespace(name=name, arguments=args))
        for name, args in (calls or [])
    ]
    return SimpleNamespace(
        message=SimpleNamespace(content=content, tool_calls=tool_calls),
        prompt_eval_count=prompt_tokens,
        eval_count=completion_tokens,
    )


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        return response() if callable(response) else response


def _run(client, *, tools, max_iterations=6, execute=None):
    records = []
    execute = execute or (lambda name, args: f"result for {name}")
    def execute_with_snapshot(name, args, *, workspace_confined=None):
        return execute(name, args)
    with patch.object(brain_ollama, "_client", return_value=client), \
         patch.object(brain_ollama, "get_best_available", side_effect=lambda model: model), \
         patch.object(brain_ollama, "_fits_local", side_effect=lambda prompt, model: model), \
         patch.object(brain_ollama, "_execute_agent_tool", side_effect=execute_with_snapshot), \
         patch.object(brain_ollama.mem, "get_context", return_value="PRIVATE MEMORY"), \
         patch.object(brain_ollama.context_budget, "target_tokens_for", return_value=16_000), \
         patch.object(brain_ollama.usage_tracker, "record", side_effect=lambda **kw: records.append(kw)), \
         patch.dict("os.environ", {"JARVIS_ALLOW_NETWORK_AGENT_TOOLS": "1"}):
        chunks = list(brain_ollama.ask_local_with_tools(
            "do the task", model="glm-4.7-flash", tools=tools,
            max_iterations=max_iterations,
        ))
    return chunks, records


def test_no_tool_answer_uses_one_provider_call_without_duplicate_synthesis():
    client = _FakeClient([_response("Final answer.")])

    chunks, records = _run(client, tools=["read_file"])

    assert chunks == ["Final answer."]
    assert len(client.calls) == 1
    assert len(records) == 1
    meta = records[0]["metadata"]["tool_loop"]
    assert meta["exit_reason"] == "model_answer"
    assert meta["call_type"] == "decision"
    assert meta["governor_eligible"] is True
    assert meta["governor_applied"] is True
    assert meta["error"] is False
    assert meta["governor"]["applied"] is True


def test_one_tool_round_then_answer_uses_two_calls_and_records_names_only():
    client = _FakeClient([
        _response(calls=[("read_file", {"path": "README.md"})]),
        _response("The file was reviewed."),
    ])

    chunks, records = _run(client, tools=["read_file"])

    assert chunks == ["The file was reviewed."]
    assert len(client.calls) == 2
    assert len(records) == 2
    first_meta = records[0]["metadata"]["tool_loop"]
    assert first_meta["tool_names"] == ["read_file"]
    assert "README.md" not in str(first_meta)
    assert client.calls[1]["messages"][-1]["role"] == "tool"


def test_partial_provider_counts_leave_total_for_estimation():
    client = _FakeClient([_response("Final answer.", completion_tokens=None)])

    _chunks, records = _run(client, tools=["read_file"])

    assert records[0]["prompt_tokens"] == 10
    assert records[0]["completion_tokens"] is None
    assert records[0]["total_tokens"] is None
    assert records[0]["estimated"] is True


def test_tool_result_cap_preserves_head_and_tail():
    text = "HEAD" + ("x" * 20_000) + "TAIL"

    compact, removed = brain_ollama._truncate_tool_result(text)

    assert len(compact) <= brain_ollama._TOOL_RESULT_MAX_CHARS
    assert compact.startswith("HEAD")
    assert compact.endswith("TAIL")
    assert removed > 0


def test_read_file_is_root_confined_and_bounded(tmp_path):
    large_file = tmp_path / "large.txt"
    large_file.write_text("x" * 40_000)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inside")

    with patch.object(brain_ollama, "_jarvis_root", return_value=tmp_path):
        result = brain_ollama._execute_agent_tool("read_file", {"path": "large.txt"})
        denied = brain_ollama._execute_agent_tool("read_file", {"path": "../outside.txt"})
        confined = brain_ollama._execute_agent_tool(
            "read_file", {"path": "inside.txt"}, workspace_confined=True,
        )
        confined_root_file = brain_ollama._execute_agent_tool(
            "read_file", {"path": "large.txt"}, workspace_confined=True,
        )

    assert len(result) < 33_000
    assert result.endswith("[... file read truncated ...]")
    assert denied == "Access denied: path outside project root."
    assert confined == "inside"
    assert confined_root_file == "File not found: large.txt"


def test_compaction_drops_only_complete_old_rounds_and_preserves_latest():
    huge = "old-head" + ("x" * 20_000) + "old-tail"
    latest = "latest-head" + ("y" * 7_000) + "latest-tail"
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "request"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "a", "arguments": {}}}]},
        {"role": "tool", "content": huge},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "b", "arguments": {}}}]},
        {"role": "tool", "content": latest},
    ]

    compact, report = brain_ollama._compact_tool_loop_messages(
        messages, [], target_tokens=2_500, reserve_response_tokens=500,
    )

    assert compact[0] == messages[0]
    assert compact[1] == messages[1]
    assert compact[-1]["content"] == latest
    assert report["dropped_tool_round_count"] == 1
    rounds = brain_ollama._tool_round_ranges(compact)
    assert all(len(round_indexes) >= 2 for round_indexes in rounds)


def test_duplicate_tool_call_is_not_executed_twice():
    duplicate = ("read_file", {"path": "README.md"})
    client = _FakeClient([
        _response(calls=[duplicate]),
        _response(calls=[duplicate]),
    ])
    executed = []

    chunks, _records = _run(
        client,
        tools=["read_file"],
        execute=lambda name, args: executed.append((name, args)) or "ok",
    )

    assert len(executed) == 1
    assert "invalid or repeated" in chunks[-1]


def test_same_read_is_allowed_after_an_intervening_write():
    client = _FakeClient([
        _response(calls=[("read_file", {"path": "README.md"})]),
        _response(calls=[("write_file", {"filepath": "README.md", "content": "new"})]),
        _response(calls=[("read_file", {"path": "README.md"})]),
        _response("Verified after write."),
    ])
    executed = []

    chunks, _records = _run(
        client,
        tools=["read_file", "write_file"],
        execute=lambda name, args: executed.append(name) or "ok",
    )

    assert chunks == ["Verified after write."]
    assert executed == ["read_file", "write_file", "read_file"]


def test_iteration_limit_runs_one_bounded_synthesis_and_marks_exhaustion():
    client = _FakeClient([
        _response(calls=[("read_file", {"path": "README.md"})]),
        [_response("Final bounded summary.", prompt_tokens=20, completion_tokens=6)],
    ])

    chunks, records = _run(client, tools=["read_file"], max_iterations=1)

    assert chunks == ["Final bounded summary."]
    assert len(client.calls) == 2
    synthesis = records[-1]["metadata"]["tool_loop"]
    assert synthesis["call_type"] == "synthesis"
    assert synthesis["max_iteration_exhausted"] is True
    assert synthesis["exit_reason"] == "max_iterations"


def test_tool_call_limit_synthesizes_accumulated_evidence():
    decisions = [
        _response(calls=[("read_file", {"path": f"file-{index}-{item}.txt"})
                         for item in range(4)])
        for index in range(3)
    ]
    client = _FakeClient([
        *decisions,
        [_response("Call limit summary.", prompt_tokens=20, completion_tokens=5)],
    ])

    chunks, records = _run(client, tools=["read_file"])

    assert chunks == ["Call limit summary."]
    assert len(client.calls) == 4
    synthesis = records[-1]["metadata"]["tool_loop"]
    assert synthesis["exit_reason"] == "tool_call_limit"
    assert synthesis["max_iteration_exhausted"] is False


def test_telemetry_failure_does_not_suppress_valid_answer():
    client = _FakeClient([_response("Valid answer.")])
    with patch.object(brain_ollama, "_client", return_value=client), \
         patch.object(brain_ollama, "get_best_available", side_effect=lambda model: model), \
         patch.object(brain_ollama, "_fits_local", side_effect=lambda prompt, model: model), \
         patch.object(brain_ollama.mem, "get_context", return_value=""), \
         patch.object(brain_ollama.context_budget, "target_tokens_for", return_value=16_000), \
         patch.object(brain_ollama.usage_tracker, "record", side_effect=OSError("disk full")):
        chunks = list(brain_ollama.ask_local_with_tools(
            "answer", model="glm-4.7-flash", tools=["read_file"],
        ))

    assert chunks == ["Valid answer."]


def test_explicit_num_ctx_caps_tool_loop_target():
    client = _FakeClient([_response("DeepSeek answer.")])
    records = []
    # Pin DEEPSEEK_CTX so the capping behaviour under test is independent of
    # the runtime default (now 32768) and any .env override.
    with patch.dict("os.environ", {"DEEPSEEK_CTX": "8192"}), \
         patch.object(brain_ollama, "_client", return_value=client), \
         patch.object(brain_ollama, "get_best_available", return_value="deepseek-r1:14b"), \
         patch.object(brain_ollama, "_fits_local", return_value="deepseek-r1:14b"), \
         patch.object(brain_ollama.mem, "get_context", return_value=""), \
         patch.object(brain_ollama.context_budget, "target_tokens_for", return_value=16_000), \
         patch.object(brain_ollama.usage_tracker, "record", side_effect=lambda **kw: records.append(kw)):
        chunks = list(brain_ollama.ask_local_with_tools(
            "answer", model="deepseek-r1:14b", tools=["read_file"],
        ))

    assert chunks == ["DeepSeek answer."]
    governor = records[0]["metadata"]["tool_loop"]["governor"]
    assert governor["target_tokens"] == 8_192


def test_tool_loop_sets_explicit_num_ctx_for_other_models():
    client = _FakeClient([_response("Llama answer.")])
    with patch.object(brain_ollama, "_client", return_value=client), \
         patch.object(brain_ollama, "get_best_available", return_value="llama3.1:8b"), \
         patch.object(brain_ollama, "_fits_local", return_value="llama3.1:8b"), \
         patch.object(brain_ollama.mem, "get_context", return_value=""), \
         patch.object(brain_ollama.context_budget, "target_tokens_for", return_value=16_000), \
         patch.object(brain_ollama.usage_tracker, "record"):
        chunks = list(brain_ollama.ask_local_with_tools(
            "answer", model="llama3.1:8b", tools=["read_file"],
        ))

    assert chunks == ["Llama answer."]
    assert client.calls[0]["options"]["num_ctx"] == 16_000


def test_web_search_cannot_follow_sensitive_file_read():
    client = _FakeClient([
        _response(calls=[("read_file", {"path": "private.txt"})]),
        _response(calls=[("web_search", {"query": "copied private data"})]),
        _response("Blocked safely."),
    ])
    executed = []

    chunks, _records = _run(
        client,
        tools=["read_file", "web_search"],
        execute=lambda name, args: executed.append(name) or "sensitive result",
    )

    assert chunks == ["Blocked safely."]
    assert executed == ["read_file"]
    assert "PRIVATE MEMORY" not in client.calls[0]["messages"][0]["content"]


def test_weather_is_guarded_as_network_tool_and_receives_no_memory():
    client = _FakeClient([
        _response(calls=[("memory_lookup", {"query": "home"})]),
        _response(calls=[("get_weather", {"location": "private address"})]),
        _response("Blocked safely."),
    ])
    executed = []

    chunks, _records = _run(
        client,
        tools=["memory_lookup", "get_weather"],
        execute=lambda name, args: executed.append(name) or "sensitive result",
    )

    assert chunks == ["Blocked safely."]
    assert executed == ["memory_lookup"]
    assert "PRIVATE MEMORY" not in client.calls[0]["messages"][0]["content"]


def test_over_budget_request_fails_closed_before_provider_call():
    client = _FakeClient([_response("should not run")])
    with patch.object(brain_ollama, "_client", return_value=client), \
         patch.object(brain_ollama, "get_best_available", side_effect=lambda model: model), \
         patch.object(brain_ollama, "_fits_local", side_effect=lambda prompt, model: model), \
         patch.object(brain_ollama.mem, "get_context", return_value=""), \
         patch.object(brain_ollama.context_budget, "target_tokens_for", return_value=2_049):
        chunks = list(brain_ollama.ask_local_with_tools(
            "x" * 1_000, model="glm-4.7-flash", tools=["read_file"],
        ))

    assert chunks == ["The local tool request is too large for its safe context budget."]
    assert client.calls == []


def test_remote_ollama_requires_explicit_opt_in_and_cloud_tags_are_rejected():
    assert brain_ollama._ollama_host_is_local("http://127.0.0.1:11434")
    assert brain_ollama._ollama_host_is_local("localhost:11434")
    assert brain_ollama._ollama_host_is_local("http://host.docker.internal:11434")
    assert not brain_ollama._ollama_host_is_local("https://ollama.example.com")
    assert brain_ollama._is_cloud_tagged_model("qwen3.5:cloud")
    assert brain_ollama._is_cloud_tagged_model("qwen3-coder:480b-cloud")
    assert not brain_ollama._is_cloud_tagged_model("cloud-native:latest")


def test_remote_ollama_policy_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "https://ollama.example.com")
    monkeypatch.delenv("JARVIS_ALLOW_REMOTE_OLLAMA", raising=False)
    with pytest.raises(RuntimeError):
        brain_ollama._enforce_ollama_host_policy()

    # Both ALLOW_REMOTE_OLLAMA and TRUSTED_OLLAMA_HOSTS are required
    monkeypatch.setenv("JARVIS_ALLOW_REMOTE_OLLAMA", "1")
    monkeypatch.setenv("JARVIS_TRUSTED_OLLAMA_HOSTS", "ollama.example.com")
    brain_ollama._enforce_ollama_host_policy()

    monkeypatch.setenv("JARVIS_TRUSTED_OLLAMA_HOSTS", "other.example.com")
    with pytest.raises(RuntimeError, match="TRUSTED_OLLAMA_HOSTS"):
        brain_ollama._enforce_ollama_host_policy()

    monkeypatch.setenv("OLLAMA_HOST", "http://ollama.example.com:11434")
    monkeypatch.setenv("JARVIS_TRUSTED_OLLAMA_HOSTS", "ollama.example.com")
    with pytest.raises(RuntimeError, match="requires HTTPS"):
        brain_ollama._enforce_ollama_host_policy()
    monkeypatch.setenv("JARVIS_ALLOW_INSECURE_REMOTE_OLLAMA", "1")
    brain_ollama._enforce_ollama_host_policy()


def test_network_agent_tools_require_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("JARVIS_ALLOW_NETWORK_AGENT_TOOLS", raising=False)
    assert brain_ollama._network_agent_tools_enabled() is False

    monkeypatch.setenv("JARVIS_ALLOW_NETWORK_AGENT_TOOLS", "true")
    assert brain_ollama._network_agent_tools_enabled() is True


def test_local_model_listing_excludes_cloud_tags():
    models = [
        SimpleNamespace(model="llama3.1:8b"),
        SimpleNamespace(model="llava:13b-cloud"),
        SimpleNamespace(model="nomic-embed-text:cloud"),
        SimpleNamespace(model="cloud/glm-5.2:latest"),
    ]
    client = SimpleNamespace(list=lambda: SimpleNamespace(models=models))

    with patch.object(brain_ollama, "_client", return_value=client):
        assert brain_ollama.list_local_models() == ["llama3.1:8b"]


def test_exact_model_lookup_never_substitutes_fallback():
    models = [SimpleNamespace(model="glm-4.7-flash:latest")]
    client = SimpleNamespace(list=lambda: SimpleNamespace(models=models))

    with patch.object(brain_ollama, "_client", return_value=client):
        with pytest.raises(RuntimeError, match="Exact local model is unavailable"):
            brain_ollama._exact_available_model("glm-5.2")


def test_strict_eval_call_excludes_memory_and_marks_remote_usage(monkeypatch):
    client = _FakeClient([[_response("Evaluation answer.")]])
    records = []
    monkeypatch.setenv("OLLAMA_HOST", "https://ollama.example.com")
    with patch.object(brain_ollama, "_client", return_value=client), \
         patch.object(brain_ollama, "_exact_available_model", return_value="glm-5.2"), \
         patch.object(brain_ollama.mem, "get_context", return_value="PRIVATE MEMORY"), \
         patch.object(brain_ollama.usage_tracker, "record", side_effect=lambda **kw: records.append(kw)):
        answer = brain_ollama.ask_local(
            "Evaluate this answer.",
            model="glm-5.2",
            strict_model=True,
            raise_on_error=True,
            include_memory=False,
        )

    assert answer == "Evaluation answer."
    assert "PRIVATE MEMORY" not in str(client.calls[0]["messages"])
    assert records[0]["local"] is False
    assert records[0]["metadata"]["endpoint_scope"] == "remote_trusted"
