"""Configuration for the Jarvis V2 local model plane."""

from __future__ import annotations

import ipaddress
import urllib.parse
from dataclasses import dataclass


class LocalConfigurationError(ValueError):
    """Raised when V2 is configured to leave the local machine."""


@dataclass(frozen=True)
class LocalModelConfig:
    """Connection details for a loopback-only OpenAI-compatible MLX server."""

    base_url: str = "http://127.0.0.1:8080/v1"
    model: str = "mlx-community/Qwen3-8B-4bit"
    request_timeout_seconds: float = 120.0
    max_output_tokens: int = 1024
    temperature: float = 0.0
    enable_thinking: bool = False

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme != "http" or not parsed.hostname:
            raise LocalConfigurationError("V2 model URL must be loopback HTTP")
        try:
            host = ipaddress.ip_address(parsed.hostname)
        except ValueError as exc:
            raise LocalConfigurationError(
                "V2 model URL must use an explicit loopback IP address"
            ) from exc
        if not host.is_loopback or parsed.username or parsed.password:
            raise LocalConfigurationError("V2 model URL must remain on loopback")
        if self.request_timeout_seconds <= 0:
            raise LocalConfigurationError("request timeout must be positive")
        if self.max_output_tokens < 1:
            raise LocalConfigurationError("max output tokens must be positive")
        if not 0.0 <= self.temperature <= 2.0:
            raise LocalConfigurationError("temperature must be between 0 and 2")

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    @property
    def models_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/models"
