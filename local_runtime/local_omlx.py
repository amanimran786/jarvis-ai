"""
local_omlx.py — oMLX inference backend client for Jarvis.

oMLX is an Apple Silicon inference server with SSD KV cache, reducing
first-token latency from 30-90s to <5s on long-context requests.
Runs as a separate process on port 11435 (OpenAI-compatible API).

https://github.com/jundot/omlx
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional

OMLX_BASE_URL = "http://localhost:11435"
OMLX_TIMEOUT_HEALTH = 3      # seconds
OMLX_TIMEOUT_GENERATE = 120  # seconds


def is_running() -> bool:
    """Return True if oMLX server is reachable on port 11435."""
    try:
        req = urllib.request.Request(f"{OMLX_BASE_URL}/v1/models")
        with urllib.request.urlopen(req, timeout=OMLX_TIMEOUT_HEALTH) as response:
            return response.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return False
    except Exception:
        return False


def status() -> dict[str, Any]:
    """
    Return {
        "running": bool,
        "url": str,
        "models": list[str],  # models loaded in oMLX
        "error": str | None,
    }
    """
    if not is_running():
        return {
            "running": False,
            "url": OMLX_BASE_URL,
            "models": [],
            "error": "oMLX server not reachable on port 11435",
        }

    try:
        models = list_models()
        return {
            "running": True,
            "url": OMLX_BASE_URL,
            "models": models,
            "error": None,
        }
    except Exception as e:
        return {
            "running": False,
            "url": OMLX_BASE_URL,
            "models": [],
            "error": str(e)[:120],
        }


def list_models() -> list[str]:
    """Return list of model names currently loaded in oMLX. Empty list if not running."""
    if not is_running():
        return []

    try:
        req = urllib.request.Request(f"{OMLX_BASE_URL}/v1/models")
        with urllib.request.urlopen(req, timeout=OMLX_TIMEOUT_HEALTH) as response:
            data = json.loads(response.read().decode("utf-8"))
            # oMLX returns OpenAI-format: {"data": [{"id": "model-name"}, ...]}
            models = [item["id"] for item in data.get("data", [])]
            return models
    except Exception:
        return []


def chat(
    model: str,
    messages: list[dict],
    *,
    temperature: float = 0.7,
    max_tokens: int = 1024,
    stream: bool = False,
    timeout: int = OMLX_TIMEOUT_GENERATE,
) -> dict[str, Any] | None:
    """
    Send a chat completion request to oMLX.
    Returns OpenAI-format response dict, or None if oMLX not available.
    Non-streaming only (stream=False path; stream=True raises NotImplementedError for now).
    """
    if not is_running():
        return None

    if stream:
        raise NotImplementedError("oMLX streaming not yet supported")

    try:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{OMLX_BASE_URL}/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def is_better_for_long_context(num_tokens_estimate: int) -> bool:
    """
    Return True if oMLX should be preferred over Ollama for this request.
    Heuristic: prefer oMLX when estimated context > 8000 tokens AND oMLX is running.
    """
    return is_running() and num_tokens_estimate > 8000
