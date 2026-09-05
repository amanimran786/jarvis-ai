"""Credential-free client for the loopback MLX-LM server."""

from __future__ import annotations

import json
import http.client
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .config import LocalModelConfig


class LocalModelError(RuntimeError):
    """Raised when the local model endpoint fails or returns invalid data."""


class LocalModelCancelled(LocalModelError):
    """Raised when the owner cancels an in-flight local model request."""


class LocalModelDeadlineExceeded(LocalModelError):
    """Raised when an in-flight local model request exhausts its agent deadline."""


@dataclass(frozen=True)
class ModelTurn:
    content: str
    tool_calls: tuple[dict[str, Any], ...]
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    request_started_at: float | None = None
    first_delta_at: float | None = None
    terminal_at: float | None = None
    completed_at: float | None = None

    @property
    def time_to_first_delta_seconds(self) -> float | None:
        if self.request_started_at is None or self.first_delta_at is None:
            return None
        return self.first_delta_at - self.request_started_at

    @property
    def request_seconds(self) -> float:
        if self.request_started_at is None or self.completed_at is None:
            return 0.0
        return self.completed_at - self.request_started_at

    @property
    def generation_seconds(self) -> float:
        if self.first_delta_at is None or self.terminal_at is None:
            return 0.0
        return self.terminal_at - self.first_delta_at


class ModelClient(Protocol):
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelTurn: ...


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Prevent a loopback request from being redirected to another destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LocalMLXClient:
    """Minimal HTTP client that never sends credentials or contacts a remote host."""

    def __init__(
        self,
        config: LocalModelConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._clock = clock
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _RejectRedirects(),
        )

    def ready(self) -> bool:
        request = urllib.request.Request(self.config.models_url, method="GET")
        try:
            with self._opener.open(
                request,
                timeout=min(self.config.request_timeout_seconds, 5.0),
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            return False
        return any(
            isinstance(item, dict) and item.get("id") == self.config.model
            for item in payload["data"]
        )

    @staticmethod
    def _merge_tool_call_deltas(
        accumulator: dict[int, dict[str, Any]],
        fragments: list[Any],
    ) -> None:
        for fragment in fragments:
            if not isinstance(fragment, dict):
                raise LocalModelError("local model returned an invalid tool-call delta")
            index = fragment.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise LocalModelError("local model returned an invalid tool-call index")
            current = accumulator.setdefault(
                index,
                {"id": "", "type": "", "function": {"name": "", "arguments": ""}},
            )
            for field_name in ("id", "type"):
                value = fragment.get(field_name)
                if value is None:
                    continue
                if not isinstance(value, str) or (
                    current[field_name] and current[field_name] != value
                ):
                    raise LocalModelError(
                        f"local model changed streamed tool-call {field_name}"
                    )
                current[field_name] = value
            function = fragment.get("function") or {}
            if not isinstance(function, dict):
                raise LocalModelError("local model returned an invalid tool-call function")
            name = function.get("name")
            if name is not None:
                if not isinstance(name, str) or (
                    current["function"]["name"]
                    and current["function"]["name"] != name
                ):
                    raise LocalModelError("local model changed streamed tool-call name")
                current["function"]["name"] = name
            arguments = function.get("arguments", "")
            if not isinstance(arguments, str):
                raise LocalModelError("local model returned invalid tool-call arguments")
            current["function"]["arguments"] += arguments

    def _payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "model": self.config.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
            "chat_template_kwargs": {
                "enable_thinking": self.config.enable_thinking,
            },
            "stream": True,
            "stream_options": {"include_usage": True},
        }

    def _consume_sse(self, response: Any, *, started: float) -> ModelTurn:
        first_delta_at: float | None = None
        terminal_at: float | None = None
        content_parts: list[str] = []
        tool_calls_by_index: dict[int, dict[str, Any]] = {}
        finish_reason = ""
        prompt_tokens = 0
        completion_tokens = 0
        usage_frames = 0
        saw_done = False
        try:
            if response.headers.get_content_type() != "text/event-stream":
                raise LocalModelError("local model did not return an SSE stream")
            for raw_line in response:
                line = raw_line.decode("utf-8").strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    raise LocalModelError("local model returned malformed SSE data")
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    saw_done = True
                    break
                chunk = json.loads(data)
                if chunk["model"] != self.config.model:
                    raise LocalModelError(
                        "local model response identity does not match the configured model"
                    )
                choices = chunk.get("choices")
                if not isinstance(choices, list):
                    raise LocalModelError("local model returned invalid stream choices")
                if not choices:
                    usage = chunk.get("usage")
                    if not isinstance(usage, dict) or usage_frames:
                        raise LocalModelError("local model returned invalid stream usage")
                    counts = {
                        name: usage.get(name)
                        for name in (
                            "prompt_tokens",
                            "completion_tokens",
                            "total_tokens",
                        )
                    }
                    if any(
                        isinstance(value, bool)
                        or not isinstance(value, int)
                        or value < 0
                        for value in counts.values()
                    ):
                        raise LocalModelError("local model returned invalid token usage")
                    if counts["total_tokens"] != (
                        counts["prompt_tokens"] + counts["completion_tokens"]
                    ):
                        raise LocalModelError("local model returned inconsistent token usage")
                    prompt_tokens = counts["prompt_tokens"]
                    completion_tokens = counts["completion_tokens"]
                    usage_frames += 1
                    continue
                if len(choices) != 1 or terminal_at is not None:
                    raise LocalModelError("local model returned invalid stream choices")
                choice = choices[0]
                if not isinstance(choice, dict):
                    raise LocalModelError("local model returned invalid stream choice")
                delta = choice.get("delta") or {}
                if not isinstance(delta, dict):
                    raise LocalModelError("local model returned invalid stream delta")
                content = delta.get("content") or ""
                streamed_tool_calls = delta.get("tool_calls") or []
                reasoning = delta.get("reasoning") or ""
                if not isinstance(content, str) or not isinstance(
                    streamed_tool_calls, list
                ) or not isinstance(reasoning, str):
                    raise LocalModelError(
                        "local model returned invalid streamed message fields"
                    )
                if first_delta_at is None and (
                    content or streamed_tool_calls or reasoning
                ):
                    first_delta_at = self._clock()
                content_parts.append(content)
                self._merge_tool_call_deltas(
                    tool_calls_by_index,
                    streamed_tool_calls,
                )
                streamed_finish_reason = choice.get("finish_reason")
                if streamed_finish_reason is not None:
                    if streamed_finish_reason not in {"stop", "length", "tool_calls"}:
                        raise LocalModelError(
                            "local model returned an invalid finish reason"
                        )
                    finish_reason = streamed_finish_reason
                    terminal_at = self._clock()
        except LocalModelError:
            raise
        except (
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise LocalModelError("local model returned an invalid response") from exc
        except OSError as exc:
            if not saw_done and (content_parts or tool_calls_by_index):
                raise LocalModelError(
                    "local model stream ended without a completion marker"
                ) from exc
            raise

        finished = self._clock()
        if not saw_done:
            raise LocalModelError("local model stream ended before its completion marker")
        if terminal_at is None:
            raise LocalModelError("local model stream ended without a terminal reason")
        if usage_frames != 1:
            raise LocalModelError("local model stream ended without valid token usage")
        tool_calls = tuple(tool_calls_by_index[index] for index in sorted(tool_calls_by_index))
        return ModelTurn(
            content="".join(content_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            request_started_at=started,
            first_delta_at=first_delta_at,
            terminal_at=terminal_at,
            completed_at=finished,
        )

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelTurn:
        request = urllib.request.Request(
            self.config.chat_completions_url,
            data=json.dumps(self._payload(messages, tools)).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = self._clock()
        try:
            with self._opener.open(
                request,
                timeout=self.config.request_timeout_seconds,
            ) as response:
                return self._consume_sse(response, started=started)
        except LocalModelError:
            raise
        except (OSError, urllib.error.URLError) as exc:
            raise LocalModelError(f"local model request failed: {exc}") from exc

    def complete_cancellable(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        is_cancelled: Callable[[], bool],
        deadline: float,
    ) -> ModelTurn:
        """Complete through a loopback socket that cancellation can interrupt."""
        if is_cancelled():
            raise LocalModelCancelled("cancelled by owner")
        if self._clock() >= deadline:
            raise LocalModelDeadlineExceeded("time budget exhausted")

        target = urllib.parse.urlsplit(self.config.chat_completions_url)
        timeout = min(
            self.config.request_timeout_seconds,
            max(0.001, deadline - self._clock()),
        )
        connection = http.client.HTTPConnection(
            target.hostname,
            target.port,
            timeout=timeout,
        )
        stopped = threading.Event()
        interrupted: list[str] = []
        request_socket: list[socket.socket] = []

        def interrupt_when_needed() -> None:
            while not stopped.wait(0.01):
                reason = ""
                if is_cancelled():
                    reason = "cancelled"
                elif self._clock() >= deadline:
                    reason = "deadline"
                if not reason:
                    continue
                interrupted.append(reason)
                active_socket = request_socket[0] if request_socket else connection.sock
                if active_socket is not None:
                    try:
                        active_socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                connection.close()
                return

        watcher = threading.Thread(
            target=interrupt_when_needed,
            name="jarvis-v2-model-cancellation",
            daemon=True,
        )
        watcher.start()
        started = self._clock()
        try:
            body = json.dumps(self._payload(messages, tools)).encode("utf-8")
            connection.request(
                "POST",
                target.path,
                body=body,
                headers={"Content-Type": "application/json"},
            )
            if connection.sock is not None:
                request_socket.append(connection.sock)
            response = connection.getresponse()
            return self._consume_sse(response, started=started)
        except LocalModelError as exc:
            reason = interrupted[-1] if interrupted else ""
            if reason == "cancelled" or is_cancelled():
                raise LocalModelCancelled("cancelled by owner") from exc
            if reason == "deadline" or self._clock() >= deadline:
                raise LocalModelDeadlineExceeded("time budget exhausted") from exc
            raise
        except (OSError, http.client.HTTPException) as exc:
            reason = interrupted[-1] if interrupted else ""
            if reason == "cancelled" or is_cancelled():
                raise LocalModelCancelled("cancelled by owner") from exc
            if reason == "deadline" or self._clock() >= deadline:
                raise LocalModelDeadlineExceeded("time budget exhausted") from exc
            raise LocalModelError(f"local model request failed: {exc}") from exc
        finally:
            stopped.set()
            connection.close()
