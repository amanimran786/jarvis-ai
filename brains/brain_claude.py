import logging
import anthropic
from config import ANTHROPIC_API_KEY, HAIKU, SYSTEM_PROMPT
import memory as mem
import conversation_context as ctx
import usage_tracker
from brains import _postprocess
from brains import _retry

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


def _get_client() -> "anthropic.Anthropic":
    """Return a live client, re-resolving the key if it wasn't set at import time.

    Falls back to reading .env directly so processes launched from environments
    that pre-set ANTHROPIC_API_KEY='' (e.g. CI, Claude Code sandbox) still work.
    """
    global client
    if client is not None:
        return client
    import os
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        # env var is absent or empty — read .env directly
        try:
            from dotenv import dotenv_values
            from pathlib import Path
            for candidate in (
                Path(__file__).resolve().parent.parent / ".env",
                Path.home() / "jarvis-ai" / ".env",
            ):
                if candidate.is_file():
                    key = (dotenv_values(candidate).get("ANTHROPIC_API_KEY") or "").strip()
                    if key:
                        break
        except Exception:
            logging.debug("[BrainClaude] silent failure in _get_client", exc_info=True)
    if key:
        client = anthropic.Anthropic(api_key=key)
    return client


def ask_claude(
    user_input: str,
    model: str = HAIKU,
    system: str = None,
    system_extra: str = "",
    track_context: bool = False,
) -> str:
    return "".join(
        ask_claude_stream(
            user_input,
            model,
            system=system,
            system_extra=system_extra,
            track_context=track_context,
        )
    )


def _strip_markdown(text: str) -> str:
    """Backward-compatible wrapper around the shared brain postprocess.

    Delegates to brains._postprocess.strip_markdown so cloud responses get
    the same think-block + markdown cleanup as local Ollama responses.
    """
    return _postprocess.strip_markdown(text)


def ask_claude_stream(
    user_input: str,
    model: str = HAIKU,
    system: str = None,
    system_extra: str = "",
    track_context: bool = False,
):
    _client = _get_client()
    if _client is None:
        raise RuntimeError("Anthropic API key is not configured.")

    system_base = system if system is not None else (SYSTEM_PROMPT + mem.get_context())
    if track_context:
        ctx.begin_turn(user_input)
        effective_system, messages, _ = ctx.build_prompt_state(system_base, system_extra=system_extra)
    else:
        effective_system = system_base
        if system_extra:
            effective_system += "\n\n" + system_extra
        messages = [{"role": "user", "content": user_input}]

    full_reply = ""
    final_message = None
    _attempt = 0
    while True:
        try:
            # cache_control enables Anthropic prompt caching: the system block
            # (persona + memory + grounding, ~1-4K tokens) is cached for ~5min,
            # so consecutive turns re-read it at ~10% of the input token cost.
            with _client.messages.stream(
                model=model,
                max_tokens=2048,
                system=[
                    {
                        "type": "text",
                        "text": effective_system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=messages
            ) as stream:
                buffer = ""
                for text in stream.text_stream:
                    full_reply += text
                    buffer += text
                    if any(buffer.endswith(c) for c in ('.', '!', '?', '\n')):
                        yield _strip_markdown(buffer)
                        buffer = ""

                if buffer:
                    yield _strip_markdown(buffer)
                final_message = stream.get_final_message()
            break
        except Exception as exc:
            # Retry only rate limits raised before any text streamed (a 429
            # raises on stream open); once output was yielded a retry would
            # duplicate it, so re-raise and let the router fail over. After
            # max retries the error propagates for provider failover.
            if full_reply or _attempt >= _retry.MAX_RETRIES or not _retry.is_rate_limit_error(exc):
                raise
            _retry.sleep_before_retry("anthropic", _attempt, exc)
            _attempt += 1

    cleaned_reply = _strip_markdown(full_reply)
    usage = getattr(final_message, "usage", None) if final_message is not None else None
    usage_tracker.record(
        provider="anthropic",
        model=model,
        local=False,
        source="brain_claude.ask_claude_stream",
        prompt_tokens=getattr(usage, "input_tokens", None) if usage else None,
        completion_tokens=getattr(usage, "output_tokens", None) if usage else None,
        total_tokens=(
            (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "output_tokens", 0) or 0)
            if usage else None
        ),
        messages=[{"role": "system", "content": effective_system}] + messages,
        response_text=cleaned_reply,
        estimated=usage is None,
        metadata={"track_context": track_context},
    )

    if track_context:
        ctx.end_turn(cleaned_reply)
