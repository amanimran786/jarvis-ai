from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis_v2.config import LocalConfigurationError
from jarvis_v2.model import LocalModelCancelled, ModelTurn
from jarvis_v2.team import AcceptanceContract, AgentAssignment, LocalAgentTeam
from jarvis_v2.tools import model_tool_schemas
from scripts import v2_dashboard, v2_trace


class _RedirectHandler(BaseHTTPRequestHandler):
    target = ""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(302)
        self.send_header("Location", self.target)
        self.end_headers()


class _TargetHandler(BaseHTTPRequestHandler):
    requests = 0

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        type(self).requests += 1
        body = b'{"data": []}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _server(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_dashboard_probe_refuses_redirects() -> None:
    target, target_thread = _server(_TargetHandler)
    redirect, redirect_thread = _server(_RedirectHandler)
    _TargetHandler.requests = 0
    _RedirectHandler.target = f"http://127.0.0.1:{target.server_port}/models"
    try:
        config = v2_dashboard.LocalModelConfig(
            base_url=f"http://127.0.0.1:{redirect.server_port}/v1"
        )
        result = v2_dashboard.probe_model_server(config)
    finally:
        redirect.shutdown()
        target.shutdown()
        redirect_thread.join(timeout=2)
        target_thread.join(timeout=2)

    assert result["up"] is False
    assert _TargetHandler.requests == 0


def test_dashboard_probe_requires_expected_model_identity() -> None:
    target, thread = _server(_TargetHandler)
    try:
        result = v2_dashboard.probe_model_server(
            v2_dashboard.LocalModelConfig(
                base_url=f"http://127.0.0.1:{target.server_port}/v1"
            )
        )
    finally:
        target.shutdown()
        thread.join(timeout=2)

    assert result["up"] is False
    assert result["expected_model"] == v2_dashboard.LocalModelConfig().model


def test_v2_tool_contract_is_self_contained_and_read_only() -> None:
    schemas = {
        item["function"]["name"]: item["function"]["parameters"]
        for item in model_tool_schemas()
    }

    assert schemas["file"]["properties"]["action"]["enum"] == ["read"]
    assert "content" not in schemas["file"]["properties"]
    assert schemas["git"]["properties"]["action"]["enum"] == [
        "status",
        "diff",
        "log",
        "branch",
        "show",
    ]
    assert "message" not in schemas["git"]["properties"]
    assert "tool_registry" not in (Path("jarvis_v2") / "tools.py").read_text()


def test_dashboard_rejects_non_loopback_endpoint() -> None:
    with pytest.raises(LocalConfigurationError):
        v2_dashboard.LocalModelConfig(base_url="https://example.com/v1")


def test_dashboard_store_rejects_symlinked_source_escape(tmp_path: Path) -> None:
    root = tmp_path / "state"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "runs").symlink_to(outside, target_is_directory=True)
    (outside / f"{'a' * 32}.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="escapes dashboard root"):
        v2_dashboard.Store(root).list_runs()


def test_dashboard_reconstructed_turns_hide_raw_arguments_by_default() -> None:
    secret = "sensitive-path-sentinel"
    messages = [
        {
            "role": "assistant",
            "content": "working",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "file",
                        "arguments": json.dumps({"path": secret}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": secret},
    ]

    redacted = v2_dashboard.Store.reconstruct_turns(
        messages, [], [], include_content=False
    )
    visible = v2_dashboard.Store.reconstruct_turns(
        messages, [], [], include_content=True
    )

    assert secret not in json.dumps(redacted)
    assert redacted[0]["tool_calls"][0]["arguments"] is None
    assert redacted[0]["tool_calls"][0]["arguments_chars"] > 0
    assert secret in json.dumps(visible)


def test_dashboard_api_requires_process_capability_and_sets_security_headers(
    tmp_path: Path,
) -> None:
    config = v2_dashboard.LocalModelConfig(base_url="http://127.0.0.1:9/v1")
    handler = type(
        "TestHandler",
        (v2_dashboard.Handler,),
        {
            "store": v2_dashboard.Store(tmp_path),
            "model_config": config,
            "capability": "test-capability",
            "allowed_hosts": set(),
        },
    )
    server, thread = _server(handler)
    handler.allowed_hosts = {f"127.0.0.1:{server.server_port}"}
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(f"{base}/api/overview", timeout=2)
        with urllib.request.urlopen(
            f"{base}/api/overview?capability=test-capability", timeout=2
        ) as response:
            payload = json.loads(response.read())
            headers = response.headers
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert denied.value.code == 403
    assert payload["endpoint"] == config.base_url
    assert headers["Referrer-Policy"] == "no-referrer"


def test_trace_redacts_sensitive_content_by_default(tmp_path: Path) -> None:
    secret = "secret-sentinel-value"
    writer = v2_trace.TraceWriter(tmp_path, trace_id="a" * 32)
    model = v2_trace.TracingModelClient(
        _OneTurnModel(secret), writer, actor="worker-a"
    )
    tools = v2_trace.TracingToolPlane(
        lambda _name, _arguments: secret,
        writer,
        actor="worker-a",
    )

    model.complete([{"role": "user", "content": secret}], [])
    tools("file", {"path": secret})
    raw = writer.path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in raw.splitlines()]
    writer.emit(
        "run_started",
        **v2_trace._sensitive_text_fields(writer, "task", secret),
    )
    raw = writer.path.read_text(encoding="utf-8")

    assert secret not in raw
    assert any(record.get("content_sha256") for record in records)
    assert any(record.get("arguments_sha256") for record in records)
    assert any(record.get("result_sha256") for record in records)


def test_trace_readiness_failure_does_not_create_phantom_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NotReady:
        def __init__(self, _config: object) -> None:
            pass

        def ready(self) -> bool:
            return False

    monkeypatch.setattr(v2_trace, "LocalMLXClient", _NotReady)
    monkeypatch.setattr(
        sys,
        "argv",
        ["v2_trace.py", "inspect", "--trace-dir", str(tmp_path / "traces")],
    )

    assert v2_trace.main() == 2
    assert not (tmp_path / "traces").exists()


def test_trace_team_wires_labelled_factories(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Team:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def run(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                team_run_id="c" * 32,
                status="completed",
                evidence=(),
                prompt_tokens=0,
                completion_tokens=0,
            )

    monkeypatch.setattr(v2_trace, "LocalAgentTeam", _Team)
    writer = v2_trace.TraceWriter(tmp_path / "traces", trace_id="d" * 32)
    result = v2_trace.trace_team(
        "goal",
        (),
        workspace=tmp_path,
        state_dir=tmp_path / "runs",
        writer=writer,
        config=v2_trace.LocalModelConfig(),
        max_workers=1,
        max_steps=1,
    )

    assert result == 0
    assert callable(captured["model_factory_for_agent"])
    assert callable(captured["tool_factory_for_agent"])


def test_trace_sensitive_content_requires_explicit_opt_in(tmp_path: Path) -> None:
    secret = "secret-sentinel-value"
    writer = v2_trace.TraceWriter(
        tmp_path,
        trace_id="b" * 32,
        include_sensitive_content=True,
    )
    model = v2_trace.TracingModelClient(
        _OneTurnModel(secret), writer, actor="worker-a"
    )
    tools = v2_trace.TracingToolPlane(
        lambda _name, _arguments: secret,
        writer,
        actor="worker-a",
    )

    model.complete([{"role": "user", "content": secret}], [])
    tools("file", {"path": secret})

    assert secret in writer.path.read_text(encoding="utf-8")


def test_trace_forwards_cancellable_model_boundary(tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    class _CancellableModel:
        def complete_cancellable(self, messages, tools, *, is_cancelled, deadline):
            observed.update(
                messages=messages,
                tools=tools,
                is_cancelled=is_cancelled,
                deadline=deadline,
            )
            raise LocalModelCancelled("cancelled by owner")

    writer = v2_trace.TraceWriter(tmp_path, trace_id="e" * 32)
    traced = v2_trace.TracingModelClient(
        _CancellableModel(), writer, actor="worker-a"
    )
    def cancelled() -> bool:
        return True

    with pytest.raises(LocalModelCancelled):
        traced.complete_cancellable(
            [{"role": "user", "content": "inspect"}],
            [],
            is_cancelled=cancelled,
            deadline=123.0,
        )

    assert observed["is_cancelled"] is cancelled
    assert observed["deadline"] == 123.0
    records = [
        json.loads(line) for line in writer.path.read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["kind"] == "model_request_failed"
    assert records[-1]["error_type"] == "LocalModelCancelled"


class _OneTurnModel:
    def __init__(self, secret: str) -> None:
        self.secret = secret

    def ready(self) -> bool:
        return True

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelTurn:
        return ModelTurn(
            content=self.secret,
            tool_calls=(
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "file",
                        "arguments": json.dumps({"path": self.secret}),
                    },
                },
            ),
            finish_reason="tool_calls",
            prompt_tokens=5,
            completion_tokens=3,
            request_started_at=1.0,
            first_delta_at=2.0,
            terminal_at=3.0,
            completed_at=4.0,
        )


class _ToolThenAnswerModel:
    def __init__(self, actor: str) -> None:
        self.actor = actor
        self.calls = 0

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelTurn:
        self.calls += 1
        if self.actor == "synthesis" or self.calls > 1:
            return ModelTurn(content=f"done:{self.actor}", tool_calls=())
        return ModelTurn(
            content="",
            tool_calls=(
                {
                    "id": f"call-{self.actor}",
                    "function": {"name": "file", "arguments": "{}"},
                },
            ),
        )


def test_team_observer_factories_receive_exact_worker_and_synthesis_ids(
    tmp_path: Path,
) -> None:
    model_actors: list[str] = []
    tool_actors: list[str] = []

    def model_for(actor: str) -> _ToolThenAnswerModel:
        model_actors.append(actor)
        return _ToolThenAnswerModel(actor)

    def tool_for(actor: str):
        tool_actors.append(actor)
        return lambda _name, _arguments: f"result:{actor}"

    team = LocalAgentTeam(
        model_factory=lambda: pytest.fail("unlabelled model factory used"),
        execute_tool=lambda _name, _arguments: pytest.fail("unlabelled tool used"),
        model_factory_for_agent=model_for,
        tool_factory_for_agent=tool_for,
        state_dir=tmp_path / "team-runs",
        max_workers=2,
    )
    result = team.run(
        goal="prove actor identity",
        assignments=[
            AgentAssignment(
                agent_id="alpha",
                role="observer",
                task="inspect alpha",
                acceptance=AcceptanceContract(required_tools=("file",)),
            ),
            AgentAssignment(
                agent_id="beta",
                role="observer",
                task="inspect beta",
                acceptance=AcceptanceContract(required_tools=("file",)),
            ),
        ],
    )

    assert result.status == "completed"
    assert sorted(model_actors) == ["alpha", "beta", "synthesis"]
    assert sorted(tool_actors) == ["alpha", "beta", "synthesis"]
    checkpoints = list((tmp_path / "team-runs" / "workers").glob("*.json"))
    assert checkpoints
    assert all(json.loads(path.read_text())["limits"] for path in checkpoints)
    team_records = [
        json.loads(line)
        for line in result.event_log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert team_records[0] == {
        "event": "team_started",
        "goal": "prove actor identity",
        "agent_ids": ["alpha", "beta"],
    }
    observed_team = v2_dashboard.Store(tmp_path).load_team(result.team_run_id)
    assert observed_team["goal"] == "prove actor identity"
    assert observed_team["goal_source"] == "recorded"
