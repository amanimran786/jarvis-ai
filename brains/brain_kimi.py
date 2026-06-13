"""
Kimi K2.7 Code brain — Moonshot's coding model via OpenRouter.

STAGED CANDIDATE, default OFF. This lane is intentionally isolated from the
hot routing path (model_router / provider_router / provider_priority): nothing
here is reachable unless JARVIS_KIMI_ENABLED is truthy AND a key is present.
The only intended caller today is kimi_eval.py, which benchmarks Kimi against
the incumbent coder models before anyone decides to wire it into a tier.

OpenRouter speaks the OpenAI wire protocol, so this reuses the OpenAI client
with a different base_url + key. Unlike brain.py this path:
  - does NOT inject the Jarvis voice persona or user memory (coding != chat),
  - does NOT strip markdown by default (that would destroy code fences),
  - does NOT run the local-first gate (it is an explicit cloud candidate).
"""

from __future__ import annotations

from config import (
    KIMI_API_KEY,
    KIMI_BASE_URL,
    KIMI_CODER,
    KIMI_ENABLED,
    KIMI_TIMEOUT_SECONDS,
)
import usage_tracker

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - import failure handled at runtime
    OpenAI = None


# Minimal coder system prompt. No persona, no memory — we are evaluating raw
# coding ability, not Jarvis's voice.
DEFAULT_CODER_SYSTEM = (
    "You are a precise senior software engineer. Answer the coding task directly. "
    "Prefer correct, runnable code and concrete debugging steps over prose."
)


def _build_client():
    if not KIMI_ENABLED:
        return None
    if OpenAI is None:
        return None
    if not KIMI_API_KEY:
        return None
    # OpenRouter's optional attribution headers — harmless, recommended by them.
    return OpenAI(
        base_url=KIMI_BASE_URL,
        api_key=KIMI_API_KEY,
        timeout=KIMI_TIMEOUT_SECONDS,
        default_headers={
            "HTTP-Referer": "https://github.com/jarvis-ai",
            "X-Title": "Jarvis (Kimi candidate eval)",
        },
    )


client = _build_client()


def is_available() -> bool:
    """True only when the flag is on, the SDK is importable, and a key exists."""
    return client is not None


def _ensure_client():
    if client is not None:
        return
    if not KIMI_ENABLED:
        raise RuntimeError(
            "Kimi is disabled. Set JARVIS_KIMI_ENABLED=1 to enable the candidate."
        )
    if OpenAI is None:
        raise RuntimeError("openai package is not installed.")
    if not KIMI_API_KEY:
        raise RuntimeError(
            "Kimi enabled but no key. Set OPENROUTER_API_KEY (or JARVIS_KIMI_API_KEY)."
        )
    raise RuntimeError("Kimi client failed to initialize.")


def ask_kimi(
    user_input: str,
    model: str = KIMI_CODER,
    system: str | None = None,
    system_extra: str = "",
) -> str:
    return "".join(
        ask_kimi_stream(
            user_input,
            model=model,
            system=system,
            system_extra=system_extra,
        )
    )


def ask_kimi_stream(
    user_input: str,
    model: str = KIMI_CODER,
    system: str | None = None,
    system_extra: str = "",
):
    _ensure_client()

    effective_system = DEFAULT_CODER_SYSTEM if system is None else system
    if system_extra:
        effective_system += ("\n\n" if effective_system else "") + system_extra
    messages = ([{"role": "system", "content": effective_system}] if effective_system else []) + [
        {"role": "user", "content": user_input}
    ]

    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        stream=True,
        stream_options={"include_usage": True},
    )

    full_reply = ""
    usage = None
    for chunk in stream:
        if getattr(chunk, "usage", None) is not None:
            usage = chunk.usage
        if not getattr(chunk, "choices", None):
            continue
        delta = chunk.choices[0].delta.content or ""
        if delta:
            full_reply += delta
            yield delta

    usage_tracker.record(
        provider="kimi",
        model=model,
        local=False,
        source="brain_kimi.ask_kimi_stream",
        prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
        completion_tokens=getattr(usage, "completion_tokens", None) if usage else None,
        total_tokens=getattr(usage, "total_tokens", None) if usage else None,
        messages=messages,
        response_text=full_reply,
        estimated=usage is None,
        metadata={"candidate": True, "lane": "kimi_eval"},
    )
