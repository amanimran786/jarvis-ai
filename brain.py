import re
from openai import OpenAI
from config import OPENAI_API_KEY, GPT_MINI, SYSTEM_PROMPT
import memory as mem
import conversation_context as ctx

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
        stream=True
    )

    full_reply = ""
    buffer = ""
    for chunk in stream:
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

    if track_context:
        ctx.end_turn(_strip_markdown(full_reply))
