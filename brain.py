import re
from openai import OpenAI
from config import OPENAI_API_KEY, GPT_MINI, SYSTEM_PROMPT, MAX_CONVERSATION_TURNS
import memory as mem

client = OpenAI(api_key=OPENAI_API_KEY)

conversation_history = []


def _strip_markdown(text: str) -> str:
    """Remove markdown artifacts that slip through despite system prompt instructions."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)       # **bold**
    text = re.sub(r'\*(.+?)\*', r'\1', text)            # *italic*
    text = re.sub(r'^\s*#{1,6}\s+', '', text, flags=re.M)  # ### headers
    text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.M)    # - bullet points
    text = re.sub(r'^\s*\d+[.)](?:\s+|$)', '', text, flags=re.M) # 1. or 1) numbered lists
    text = re.sub(r'```\w*\n?', '', text)                # code fences
    return text


def _trim_history() -> None:
    """Keep only the most recent conversation turns to control token growth."""
    max_messages = max(2, MAX_CONVERSATION_TURNS * 2)
    if len(conversation_history) > max_messages:
        del conversation_history[:-max_messages]


def ask(user_input: str, model: str = GPT_MINI) -> str:
    return "".join(ask_stream(user_input, model))


def ask_stream(user_input: str, model: str = GPT_MINI):
    conversation_history.append({"role": "user", "content": user_input})
    _trim_history()
    system = SYSTEM_PROMPT + mem.get_context()
    messages = [{"role": "system", "content": system}] + conversation_history

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

    conversation_history.append({"role": "assistant", "content": _strip_markdown(full_reply)})
    _trim_history()
