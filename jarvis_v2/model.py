"""Credential-free client for the loopback MLX-LM server."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .config import LocalModelConfig


class LocalModelError(RuntimeError):
    """Raised when the local model endpoint fails or returns invalid data."""


@dataclass(frozen=True)
class ModelTurn:
    content: str
    tool_calls: tuple[dict[str, Any], ...]


class ModelClient(Protocol):
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelTurn: ...


class LocalMLXClient:
    """Minimal HTTP client that never sends credentials or contacts a remote host."""

    def __init__(self, config: LocalModelConfig) -> None:
        self.config = config

    def ready(self) -> bool:
        request = urllib.request.Request(self.config.models_url, method="GET")
        try:
            with urllib.request.urlopen(  # noqa: S310 - URL is loopback-validated
                request,
                timeout=min(self.config.request_timeout_seconds, 5.0),
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return False
        return isinstance(payload, dict) and isinstance(payload.get("data"), list)

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
            "stream": False,
        }
        request = urllib.request.Request(
            self.config.chat_completions_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - URL is loopback-validated
                request,
                timeout=self.config.request_timeout_seconds,
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
            message = result["choices"][0]["message"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LocalModelError("local model returned an invalid response") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise LocalModelError(f"local model request failed: {exc}") from exc

        content = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []
        if not isinstance(content, str) or not isinstance(tool_calls, list):
            raise LocalModelError("local model returned invalid message fields")
        return ModelTurn(content=content, tool_calls=tuple(tool_calls))
