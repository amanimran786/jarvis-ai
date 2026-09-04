from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from jarvis_v2.agent import AgentLimits, AgentState, LocalAgentLoop
from jarvis_v2.config import LocalConfigurationError, LocalModelConfig
from jarvis_v2.model import LocalMLXClient, LocalModelError, ModelTurn
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
    assert len(result.tool_evidence) == 1
    assert result.tool_evidence[0].tool == "git"
    assert result.tool_evidence[0].result_chars == len("tracked files are clean")
    assert loop.load(result.run_id).messages[-1] == {
        "role": "assistant",
        "content": "Repository is clean.",
    }
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


def test_agent_can_resume_a_cancelled_checkpoint(tmp_path: Path):
    first = LocalAgentLoop(
        model=FakeModel([]),
        execute_tool=lambda name, arguments: "unused",
        state_dir=tmp_path,
        is_cancelled=lambda: True,
    ).run("Inspect")
    second = LocalAgentLoop(
        model=FakeModel([ModelTurn(content="Resumed.", tool_calls=())]),
        execute_tool=lambda name, arguments: "unused",
        state_dir=tmp_path,
    ).run(resume_run_id=first.run_id)

    assert first.status == "cancelled"
    assert second.status == "completed"


def test_agent_converts_unexpected_model_error_to_blocked_checkpoint(tmp_path: Path):
    class BrokenModel:
        def complete(self, messages, tools):
            raise RuntimeError("adapter crashed")

    result = LocalAgentLoop(
        model=BrokenModel(),
        execute_tool=lambda name, arguments: "unused",
        state_dir=tmp_path,
        limits=AgentLimits(max_consecutive_errors=1),
    ).run("Inspect")

    assert result.status == "blocked"
    assert result.reason == "RuntimeError: adapter crashed"
    assert result.checkpoint_path.is_file()


def test_agent_can_require_real_tool_evidence(tmp_path: Path):
    result = LocalAgentLoop(
        model=FakeModel([ModelTurn(content="I inspected it.", tool_calls=())]),
        execute_tool=lambda name, arguments: "unused",
        state_dir=tmp_path,
        limits=AgentLimits(max_consecutive_errors=1),
        require_tool_evidence=True,
    ).run("Inspect")

    assert result.status == "blocked"
    assert "requires tool evidence" in result.reason


def test_checkpoint_permissions_are_owner_only(tmp_path: Path):
    result = LocalAgentLoop(
        model=FakeModel([ModelTurn(content="Done.", tool_calls=())]),
        execute_tool=lambda name, arguments: "unused",
        state_dir=tmp_path / "private",
    ).run("Inspect")

    assert (result.checkpoint_path.stat().st_mode & 0o777) == 0o600
    assert (result.event_log_path.stat().st_mode & 0o777) == 0o600
    assert (result.checkpoint_path.parent.stat().st_mode & 0o777) == 0o700


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


def test_same_run_concurrent_checkpoints_have_unique_complete_events(tmp_path: Path):
    loop = LocalAgentLoop(
        model=FakeModel([]),
        execute_tool=lambda name, arguments: "unused",
        state_dir=tmp_path,
    )
    state = AgentState(run_id="a" * 32, task="stress")

    with ThreadPoolExecutor(max_workers=8) as executor:
        paths = list(executor.map(lambda _: loop._checkpoint(state), range(100)))

    assert len(paths) == 100
    events = [json.loads(line) for line in loop._event_path(state.run_id).read_text().splitlines()]
    assert len(events) == 100
    assert [event["sequence"] for event in events] == list(range(1, 101))


def test_second_owner_cannot_resume_same_run_while_active(tmp_path: Path):
    seed = LocalAgentLoop(
        model=FakeModel([]),
        execute_tool=lambda name, arguments: "unused",
        state_dir=tmp_path,
        is_cancelled=lambda: True,
    ).run("Inspect")
    entered = threading.Event()
    release = threading.Event()

    class BlockingModel:
        def complete(self, messages, tools):
            entered.set()
            assert release.wait(timeout=2)
            return ModelTurn(content="owner completed", tool_calls=())

    owner = LocalAgentLoop(
        model=BlockingModel(),
        execute_tool=lambda name, arguments: "unused",
        state_dir=tmp_path,
    )
    contender = LocalAgentLoop(
        model=FakeModel([ModelTurn(content="contender", tool_calls=())]),
        execute_tool=lambda name, arguments: "unused",
        state_dir=tmp_path,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        future = executor.submit(owner.run, resume_run_id=seed.run_id)
        assert entered.wait(timeout=1)
        with pytest.raises(RuntimeError, match="already owned"):
            contender.run(resume_run_id=seed.run_id)
        release.set()
        assert future.result(timeout=2).answer == "owner completed"

    assert (tmp_path / f"{seed.run_id}.lock").stat().st_mode & 0o777 == 0o600


def test_resume_rejects_checkpoint_with_mismatched_internal_run_id(tmp_path: Path):
    safe_id = "a" * 32
    payload = {
        "run_id": "/tmp/escaped",
        "task": "tampered",
        "status": "blocked",
        "step": 0,
        "messages": [],
        "consecutive_errors": 0,
        "last_call_digest": "",
        "repeated_call_count": 0,
        "final_answer": "",
        "reason": "",
    }
    (tmp_path / f"{safe_id}.json").write_text(json.dumps(payload), encoding="utf-8")
    loop = LocalAgentLoop(
        model=FakeModel([]),
        execute_tool=lambda name, arguments: "unused",
        state_dir=tmp_path,
    )

    with pytest.raises(ValueError, match="does not match"):
        loop.load(safe_id)


def test_git_status_disables_repository_fsmonitor_execution(tmp_path: Path):
    subprocess.run(["/usr/bin/git", "init", "-q", str(tmp_path)], check=True, timeout=10)
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "monitor.py"
    hook.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    subprocess.run(
        ["/usr/bin/git", "-C", str(tmp_path), "config", "core.fsmonitor", str(hook)],
        check=True,
        timeout=10,
    )

    ReadOnlyLocalTools(tmp_path)("git", {"action": "status"})

    assert not marker.exists()


def test_git_diff_disables_repository_textconv_execution(tmp_path: Path):
    subprocess.run(["/usr/bin/git", "init", "-q", str(tmp_path)], check=True, timeout=10)
    tracked = tmp_path / "tracked.bin"
    tracked.write_text("before", encoding="utf-8")
    (tmp_path / ".gitattributes").write_text("*.bin diff=evil\n", encoding="utf-8")
    subprocess.run(
        ["/usr/bin/git", "-C", str(tmp_path), "add", "tracked.bin", ".gitattributes"],
        check=True,
        timeout=10,
    )
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
        timeout=10,
    )
    marker = tmp_path / "textconv-ran"
    helper = tmp_path / "textconv.py"
    helper.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    subprocess.run(
        ["/usr/bin/git", "-C", str(tmp_path), "config", "diff.evil.textconv", str(helper)],
        check=True,
        timeout=10,
    )
    tracked.write_text("after", encoding="utf-8")

    ReadOnlyLocalTools(tmp_path)("git", {"action": "diff"})

    assert not marker.exists()


def test_local_model_client_ignores_proxy_environment(monkeypatch):
    requests: list[str] = []

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"choices":[{"message":{"content":"leaked"}}]}')

        def log_message(self, format, *args):
            return

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy.server_port}")
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    closed_socket = socket.socket()
    closed_socket.bind(("127.0.0.1", 0))
    unused_port = closed_socket.getsockname()[1]
    closed_socket.close()
    client = LocalMLXClient(
        LocalModelConfig(
            base_url=f"http://127.0.0.1:{unused_port}/v1",
            request_timeout_seconds=0.2,
        )
    )

    try:
        with pytest.raises(LocalModelError):
            client.complete([{"role": "user", "content": "SENSITIVE"}], [])
    finally:
        proxy.shutdown()
        proxy.server_close()

    assert requests == []


def test_local_model_client_rejects_http_redirect():
    redirected: list[str] = []

    class SinkHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            redirected.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"data":[]}')

        def log_message(self, format, *args):
            return

    sink = ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/v1/models")
            self.end_headers()

        def log_message(self, format, *args):
            return

    origin = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (sink, origin)
    ]
    for thread in threads:
        thread.start()
    client = LocalMLXClient(
        LocalModelConfig(base_url=f"http://127.0.0.1:{origin.server_port}/v1")
    )

    try:
        assert client.ready() is False
    finally:
        origin.shutdown()
        sink.shutdown()
        origin.server_close()
        sink.server_close()

    assert redirected == []


def test_local_model_readiness_requires_configured_model_identity():
    class ModelsHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                b'{"data":[{"id":"mlx-community/Different-Model"}]}'
            )

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = LocalMLXClient(
        LocalModelConfig(base_url=f"http://127.0.0.1:{server.server_port}/v1")
    )

    try:
        assert client.ready() is False
    finally:
        server.shutdown()
        server.server_close()


def test_local_model_completion_rejects_wrong_model_identity():
    class CompletionHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                b'{"model":"mlx-community/Different-Model",'
                b'"choices":[{"finish_reason":"stop",'
                b'"message":{"content":"wrong model","tool_calls":[]}}],'
                b'"usage":{"prompt_tokens":1,"completion_tokens":1}}'
            )

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), CompletionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = LocalMLXClient(
        LocalModelConfig(base_url=f"http://127.0.0.1:{server.server_port}/v1")
    )

    try:
        with pytest.raises(LocalModelError, match="identity"):
            client.complete([{"role": "user", "content": "test"}], [])
    finally:
        server.shutdown()
        server.server_close()


def test_agent_rejects_truncated_model_answer(tmp_path: Path):
    result = LocalAgentLoop(
        model=FakeModel(
            [ModelTurn(content="unfinished", tool_calls=(), finish_reason="length")]
        ),
        execute_tool=lambda name, arguments: "unused",
        state_dir=tmp_path,
        limits=AgentLimits(max_consecutive_errors=1),
    ).run("Inspect")

    assert result.status == "blocked"
    assert "truncated" in result.reason


def test_agent_enforces_cumulative_token_budget(tmp_path: Path):
    result = LocalAgentLoop(
        model=FakeModel(
            [
                ModelTurn(
                    content="too expensive",
                    tool_calls=(),
                    prompt_tokens=8,
                    completion_tokens=8,
                )
            ]
        ),
        execute_tool=lambda name, arguments: "unused",
        state_dir=tmp_path,
        limits=AgentLimits(max_consecutive_errors=1, max_total_tokens=10),
    ).run("Inspect")

    assert result.status == "blocked"
    assert "token budget" in result.reason
