import os
import re
from openai import OpenAI
from config import OPENAI_API_KEY, GPT_MINI, SYSTEM_PROMPT, FREE_FIRST_ENABLED, LOCAL_DEFAULT
import memory as mem
import conversation_context as ctx
import usage_tracker

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


def _should_route_local() -> bool:
    """Local-first gate. True when Jarvis is in open-source mode or FREE_FIRST is on.

    Uses a lazy import of model_router to avoid the brain <-> model_router cycle.
    Falls back to FREE_FIRST_ENABLED alone if the router isn't importable yet
    (e.g. during early bootstrap). An explicit env override lets ops force it off.
    """
    if os.getenv("JARVIS_DISABLE_LOCAL_FIRST_GATE", "").strip().lower() in {"1", "true", "yes"}:
        return False
    open_source = False
    try:
        import model_router  # local import — model_router imports brain at module top
        open_source = model_router.is_open_source_mode()
    except Exception:
        # Default to FREE_FIRST_ENABLED only — same posture as provider_priority.
        pass
    return bool(FREE_FIRST_ENABLED or open_source)


def _is_open_source_strict() -> bool:
    """In strict open-source mode we must NOT fall back to OpenAI."""
    try:
        import model_router
        return model_router.is_open_source_mode()
    except Exception:
        return False


def _strip_markdown(text: str) -> str:
    """Remove markdown artifacts that slip through despite system prompt instructions."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)       # **bold**
    text = re.sub(r'\*(.+?)\*', r'\1', text)            # *italic*
    text = re.sub(r'^\s*#{1,6}\s+', '', text, flags=re.M)  # ### headers
    text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.M)    # - bullet points
    text = re.sub(r'^\s*\d+[.)](?:\s+|$)', '', text, flags=re.M) # 1. or 1) numbered lists
    text = re.sub(r'```\w*\n?', '', text)                # code fences
    return text


def ask(
    user_input: str,
    model: str = GPT_MINI,
    system_extra: str = "",
    track_context: bool = False,
    bypass_local: bool = False,
) -> str:
    return "".join(
        ask_stream(
            user_input,
            model,
            system_extra=system_extra,
            track_context=track_context,
            bypass_local=bypass_local,
        )
    )


def ask_stream(
    user_input: str,
    model: str = GPT_MINI,
    system_extra: str = "",
    track_context: bool = False,
    bypass_local: bool = False,
):
    # Local-first gate. The OpenAI lane was the largest cloud-leak source (~45%
    # of all calls historically). Route to Ollama first when local-first is on,
    # falling back to OpenAI only if local fails AND we're not in strict
    # open-source mode.
    #
    # Orchestrators (provider_priority, model_router) that already tried local
    # at the planner level pass bypass_local=True to skip a wasteful second
    # local attempt before the cloud call they explicitly chose.
    if not bypass_local and _should_route_local():
        try:
            from brains.brain_ollama import ask_local_stream
            yield from ask_local_stream(
                user_input,
                model=LOCAL_DEFAULT,
                system_extra=system_extra,
                track_context=track_context,
                raise_on_error=True,
            )
            return
        except Exception as exc:
            if _is_open_source_strict():
                raise
            print(f"[brain] local-first failed for ask_stream, falling back to OpenAI: {exc}")
            if client is None:
                raise RuntimeError(
                    "Local provider failed and OpenAI is not configured."
                ) from exc

    if client is None:
        raise RuntimeError("OpenAI API key is not configured.")

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

    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        stream=True,
        stream_options={"include_usage": True},
    )

    full_reply = ""
    buffer = ""
    usage = None
    for chunk in stream:
        if getattr(chunk, "usage", None) is not None:
            usage = chunk.usage
        if not getattr(chunk, "choices", None):
            continue
        delta = chunk.choices[0].delta.content or ""
        full_reply += delta
        buffer += delta
        # Flush on sentence boundaries to keep streaming feel
        if any(buffer.endswith(c) for c in ('.', '!', '?', '\n')):
            cleaned = _strip_markdown(buffer)
            yield cleaned
            buffer = ""

    if buffer:
        yield _strip_markdown(buffer)

    cleaned_reply = _strip_markdown(full_reply)
    usage_tracker.record(
        provider="openai",
        model=model,
        local=False,
        source="brain.ask_stream",
        prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
        completion_tokens=getattr(usage, "completion_tokens", None) if usage else None,
        total_tokens=getattr(usage, "total_tokens", None) if usage else None,
        messages=messages,
        response_text=cleaned_reply,
        estimated=usage is None,
        metadata={"track_context": track_context},
    )

    if track_context:
        ctx.end_turn(cleaned_reply)
