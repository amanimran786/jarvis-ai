import anthropic
import re
from config import ANTHROPIC_API_KEY, HAIKU, SYSTEM_PROMPT, MAX_CONVERSATION_TURNS
import memory as mem

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

conversation_history = []


def ask_claude(user_input: str, model: str = HAIKU, system: str = None) -> str:
    return "".join(ask_claude_stream(user_input, model, system=system))


def _trim_history() -> None:
    """Keep only recent turns so long sessions don't balloon token cost."""
    max_messages = max(2, MAX_CONVERSATION_TURNS * 2)
    if len(conversation_history) > max_messages:
        del conversation_history[:-max_messages]


def _strip_markdown(text: str) -> str:
    """Remove markdown artifacts because Jarvis responses are spoken aloud."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'^\s*#{1,6}\s+', '', text, flags=re.M)
    text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.M)
    text = re.sub(r'^\s*\d+[.)](?:\s+|$)', '', text, flags=re.M)
    text = re.sub(r'```\w*\n?', '', text)
    return text


def ask_claude_stream(user_input: str, model: str = HAIKU, system: str = None):
    conversation_history.append({"role": "user", "content": user_input})
    _trim_history()
    effective_system = system if system is not None else (SYSTEM_PROMPT + mem.get_context())

    full_reply = ""
    with client.messages.stream(
        model=model,
        max_tokens=2048,
        system=effective_system,
        messages=conversation_history
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

    conversation_history.append({"role": "assistant", "content": _strip_markdown(full_reply)})
    _trim_history()
