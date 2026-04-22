import anthropic
import re
from config import ANTHROPIC_API_KEY, HAIKU, SYSTEM_PROMPT
import memory as mem
import conversation_context as ctx
import usage_tracker

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


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
    """Remove markdown artifacts because Jarvis responses are spoken aloud."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'^\s*#{1,6}\s+', '', text, flags=re.M)
    text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.M)
    text = re.sub(r'^\s*\d+[.)](?:\s+|$)', '', text, flags=re.M)
    text = re.sub(r'```\w*\n?', '', text)
    return text


def ask_claude_stream(
    user_input: str,
    model: str = HAIKU,
    system: str = None,
    system_extra: str = "",
    track_context: bool = False,
):
    if client is None:
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
    with client.messages.stream(
        model=model,
        max_tokens=2048,
        system=effective_system,
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
