import re
from openai import OpenAI
from config import OPENAI_API_KEY, GPT_MINI, SYSTEM_PROMPT
import memory as mem
import conversation_context as ctx
import usage_tracker

client = OpenAI(api_key=OPENAI_API_KEY)


def _strip_markdown(text: str) -> str:
    """Remove markdown artifacts that slip through despite system prompt instructions."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)       # **bold**
    text = re.sub(r'\*(.+?)\*', r'\1', text)            # *italic*
    text = re.sub(r'^\s*#{1,6}\s+', '', text, flags=re.M)  # ### headers
    text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.M)    # - bullet points
    text = re.sub(r'^\s*\d+[.)](?:\s+|$)', '', text, flags=re.M) # 1. or 1) numbered lists
    text = re.sub(r'```\w*\n?', '', text)                # code fences
    return text


def ask(user_input: str, model: str = GPT_MINI, system_extra: str = "", track_context: bool = False) -> str:
    return "".join(ask_stream(user_input, model, system_extra=system_extra, track_context=track_context))


def ask_stream(user_input: str, model: str = GPT_MINI, system_extra: str = "", track_context: bool = False):
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
