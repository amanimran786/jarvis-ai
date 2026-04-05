"""
Local brain using Ollama — runs entirely on your Mac.
No API keys, no external servers, no restrictions, completely private.
"""

import ollama as _ollama
from config import SYSTEM_PROMPT
import memory as mem

# Default local models — change to whatever you've pulled
LOCAL_DEFAULT   = "llama3.1:8b"     # fast general purpose
LOCAL_CODER     = "deepseek-coder-v2"  # best for code
LOCAL_REASONING = "mistral"          # sharp reasoning

conversation_history = []


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

    system = SYSTEM_PROMPT + mem.get_context()
    messages = [{"role": "system", "content": system}] + conversation_history

    full_reply = ""
    try:
        stream = _ollama.chat(
            model=model,
            messages=messages,
            stream=True
        )
        for chunk in stream:
            delta = chunk.message.content or ""
            full_reply += delta
            yield delta
    except Exception as e:
        error = f"Local model error: {e}. Make sure Ollama is running: ollama serve"
        yield error
        full_reply = error

    conversation_history.append({"role": "assistant", "content": full_reply})


def list_local_models() -> list[str]:
    """Return names of all pulled local models."""
    try:
        return [m.model for m in _ollama.list().models]
    except Exception:
        return []
