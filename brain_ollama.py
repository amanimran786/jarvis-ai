"""
Local brain using Ollama — runs entirely on your Mac.
No API keys, no external servers, no restrictions, completely private.
"""

import ollama as _ollama
import re
from config import SYSTEM_PROMPT
import memory as mem
import conversation_context as ctx

# Default local models — change to whatever you've pulled
LOCAL_DEFAULT   = "llama3.1:8b"     # fast general purpose
LOCAL_CODER     = "qwen2.5-coder:7b"   # practical local coder model
LOCAL_REASONING = "mistral"          # sharp reasoning

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


def ask_local(user_input: str, model: str = LOCAL_DEFAULT, system_extra: str = "", track_context: bool = False) -> str:
    return "".join(ask_local_stream(user_input, model, system_extra=system_extra, track_context=track_context))


def ask_local_stream(user_input: str, model: str = LOCAL_DEFAULT, system_extra: str = "", track_context: bool = False):
    """Stream a response from a local Ollama model."""
    model = get_best_available(model)

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

    if track_context:
        ctx.end_turn(_strip_markdown(full_reply))


def list_local_models() -> list[str]:
    """Return names of all pulled local models."""
    try:
        return [m.model for m in _ollama.list().models]
    except Exception:
        return []
