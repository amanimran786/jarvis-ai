import anthropic
from config import ANTHROPIC_API_KEY, HAIKU, SYSTEM_PROMPT
import memory as mem

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

conversation_history = []


def ask_claude(user_input: str, model: str = HAIKU, system: str = None) -> str:
    return "".join(ask_claude_stream(user_input, model, system=system))


def ask_claude_stream(user_input: str, model: str = HAIKU, system: str = None):
    conversation_history.append({"role": "user", "content": user_input})
    effective_system = system if system is not None else (SYSTEM_PROMPT + mem.get_context())

    full_reply = ""
    with client.messages.stream(
        model=model,
        max_tokens=2048,
        system=effective_system,
        messages=conversation_history
    ) as stream:
        for text in stream.text_stream:
            full_reply += text
            yield text

    conversation_history.append({"role": "assistant", "content": full_reply})
