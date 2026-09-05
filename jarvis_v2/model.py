"""Credential-free client for the loopback MLX-LM server."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .config import LocalModelConfig


class LocalModelError(RuntimeError):
    """Raised when the local model endpoint fails or returns invalid data."""


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

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelTurn:
        payload = {
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
        request = urllib.request.Request(
            self.config.chat_completions_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = self._clock()
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
            with self._opener.open(
                request,
                timeout=self.config.request_timeout_seconds,
            ) as response:
                if not response.headers.get_content_type() == "text/event-stream":
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
        except (OSError, urllib.error.URLError) as exc:
            if not saw_done and (content_parts or tool_calls_by_index):
                raise LocalModelError(
                    "local model stream ended without a completion marker"
                ) from exc
            raise LocalModelError(f"local model request failed: {exc}") from exc

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
