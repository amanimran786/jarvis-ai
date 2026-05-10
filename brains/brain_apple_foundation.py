"""
Apple Foundation local brain via apfel's OpenAI-compatible server.

This is an optional fast tier. It only talks to localhost and is used when
apfel exposes `apple-foundationmodel`; Ollama remains the general local model.
"""

from __future__ import annotations

import json
import re
import time
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import httpx
from openai import OpenAI

from config import (
    APPLE_FOUNDATION_API_KEY,
    APPLE_FOUNDATION_BASE_URL,
    APPLE_FOUNDATION_ENABLED,
    APPLE_FOUNDATION_MAX_TOKENS,
    APPLE_FOUNDATION_MODEL,
    APPLE_FOUNDATION_TIMEOUT_SECONDS,
    SYSTEM_PROMPT,
)
import conversation_context as ctx
import memory as mem
import usage_tracker

_AVAILABILITY_TTL_SECONDS = 30.0
_availability_checked_at = 0.0
_availability_result = False


def _client() -> OpenAI:
    timeout = httpx.Timeout(
        connect=2.0,
        read=APPLE_FOUNDATION_TIMEOUT_SECONDS,
        write=5.0,
        pool=2.0,
    )
    return OpenAI(
        base_url=APPLE_FOUNDATION_BASE_URL,
        api_key=APPLE_FOUNDATION_API_KEY,
        timeout=timeout,
    )


def _models_url() -> str:
    return APPLE_FOUNDATION_BASE_URL.rstrip("/") + "/models"


def _is_loopback_base_url() -> bool:
    parsed = urlparse(APPLE_FOUNDATION_BASE_URL)
    return parsed.scheme in {"http", "https"} and parsed.hostname in {"localhost", "127.0.0.1", "::1"}


def _strip_markdown(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"^\s*#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*\d+[.)]\s*", "", text, flags=re.M)
    text = re.sub(r"```\w*\n?", "", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text.strip()


def is_available(force_refresh: bool = False) -> bool:
    """True when the local apfel server exposes the Apple Foundation model."""
    global _availability_checked_at, _availability_result
    if not APPLE_FOUNDATION_ENABLED or not _is_loopback_base_url():
        return False

    now = time.monotonic()
    if not force_refresh and (now - _availability_checked_at) < _AVAILABILITY_TTL_SECONDS:
        return _availability_result

    try:
        request = Request(_models_url(), headers={"Authorization": f"Bearer {APPLE_FOUNDATION_API_KEY}"})
        with urlopen(request, timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
        models = payload.get("data", []) if isinstance(payload, dict) else []
        _availability_result = any(item.get("id") == APPLE_FOUNDATION_MODEL for item in models if isinstance(item, dict))
    except (OSError, URLError, TimeoutError, json.JSONDecodeError):
        _availability_result = False

    _availability_checked_at = time.monotonic()
    return _availability_result


def ask_apple_foundation_stream(
    user_input: str,
    model: str = APPLE_FOUNDATION_MODEL,
    system_extra: str = "",
    track_context: bool = False,
    raise_on_error: bool = False,
):
    """Stream a short local response from Apple's on-device Foundation model."""
    if not is_available():
        error = "Apple Foundation local model is unavailable."
        if raise_on_error:
            raise RuntimeError(error)
        yield error
        return

    system_base = SYSTEM_PROMPT + mem.get_context()
    if track_context:
        ctx.begin_turn(user_input)
        system, messages, _ = ctx.build_prompt_state(system_base, system_extra=system_extra)
        messages = [{"role": "system", "content": system}] + messages
    else:
        system = system_base
        if system_extra:
            system += "\n\n" + system_extra
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user_input}]

    full_reply = ""
    usage = None
    try:
        stream = _client().chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=APPLE_FOUNDATION_MAX_TOKENS,
            stream=True,
            stream_options={"include_usage": True},
        )
        for chunk in stream:
            if getattr(chunk, "usage", None) is not None:
                usage = chunk.usage
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta.content or ""
            full_reply += delta
            if delta:
                yield delta
    except Exception as exc:
        if raise_on_error:
            raise RuntimeError(str(exc)) from exc
        yield "Apple Foundation local model failed to respond."
        return

    usage_tracker.record(
        provider="apple_foundation",
        model=model,
        local=True,
        source="brain_apple_foundation.ask_apple_foundation_stream",
        prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
        completion_tokens=getattr(usage, "completion_tokens", None) if usage else None,
        total_tokens=getattr(usage, "total_tokens", None) if usage else None,
        messages=messages,
        response_text=_strip_markdown(full_reply),
        estimated=usage is None,
        metadata={"track_context": track_context},
    )

    if track_context:
        ctx.end_turn(full_reply)


def status() -> dict:
    return {
        "enabled": APPLE_FOUNDATION_ENABLED,
        "available": is_available(),
        "base_url": APPLE_FOUNDATION_BASE_URL,
        "loopback_only": _is_loopback_base_url(),
        "model": APPLE_FOUNDATION_MODEL,
        "max_tokens": APPLE_FOUNDATION_MAX_TOKENS,
        "timeout_seconds": APPLE_FOUNDATION_TIMEOUT_SECONDS,
    }
