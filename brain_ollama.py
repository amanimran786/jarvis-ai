"""
Local brain using Ollama — runs entirely on your Mac.
No API keys, no external servers, no restrictions, completely private.
"""

import ollama as _ollama
import re
from config import SYSTEM_PROMPT, MAX_CONVERSATION_TURNS
import memory as mem

# Default local models — change to whatever you've pulled
LOCAL_DEFAULT   = "llama3.1:8b"     # fast general purpose
LOCAL_CODER     = "qwen2.5-coder:7b"   # practical local coder model
LOCAL_REASONING = "mistral"          # sharp reasoning

conversation_history = []


def _trim_history() -> None:
    """Keep only recent turns so local chats do not grow without bound."""
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


def _is_available(model: str) -> bool:
    """Check if a model is pulled and available."""
    try:
        models = [m.model for m in _ollama.list().models]
        return any(model in m for m in models)
    except Exception:
        return False


def get_best_available(preferred: str) -> str:
    """Return preferred model if available, else fall back to first available."""
    try:
        models = [m.model for m in _ollama.list().models]
        if not models:
            raise RuntimeError("No Ollama models found. Run: ollama pull llama3.1:8b")
        if any(preferred in m for m in models):
            return preferred
        return models[0]
    except Exception as e:
        raise RuntimeError(f"Ollama not running. Start it with: ollama serve\n{e}")


def ask_local(user_input: str, model: str = LOCAL_DEFAULT) -> str:
    return "".join(ask_local_stream(user_input, model))


def ask_local_stream(user_input: str, model: str = LOCAL_DEFAULT):
    """Stream a response from a local Ollama model."""
    model = get_best_available(model)
    conversation_history.append({"role": "user", "content": user_input})
    _trim_history()

    system = SYSTEM_PROMPT + mem.get_context()
    messages = [{"role": "system", "content": system}] + conversation_history

    full_reply = ""
    try:
        stream = _ollama.chat(
            model=model,
            messages=messages,
            stream=True
        )
        buffer = ""
        for chunk in stream:
            delta = chunk.message.content or ""
            full_reply += delta
            buffer += delta
            if any(buffer.endswith(c) for c in ('.', '!', '?', '\n')):
                yield _strip_markdown(buffer)
                buffer = ""
        if buffer:
            yield _strip_markdown(buffer)
    except Exception as e:
        error = f"Local model error: {e}. Make sure Ollama is running: ollama serve"
        yield error
        full_reply = error

    conversation_history.append({"role": "assistant", "content": _strip_markdown(full_reply)})
    _trim_history()


def list_local_models() -> list[str]:
    """Return names of all pulled local models."""
    try:
        return [m.model for m in _ollama.list().models]
    except Exception:
        return []
