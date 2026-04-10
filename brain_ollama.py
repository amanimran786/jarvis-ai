"""
Local brain using Ollama — runs entirely on your Mac.
No API keys, no external servers, no restrictions, completely private.
"""

import ollama as _ollama
import re
import os
import atexit
import threading
from config import SYSTEM_PROMPT, LOCAL_DEFAULT, LOCAL_CODER, LOCAL_REASONING, LOCAL_TUNED, LOCAL_PREFER_TUNED
import memory as mem
import conversation_context as ctx
import usage_tracker

# Injected for non-trivial questions to prime chain-of-thought on smaller models.
# Kept brief so it doesn't bloat the context window.
_REASONING_BOOST = (
    "Reasoning approach: before giving your final answer, identify the core question, "
    "state what you know with confidence, flag any uncertainty explicitly, then deliver "
    "your conclusion. Be precise. Speak in natural sentences — no bullets or markdown."
)

try:
    import httpx
except Exception:
    httpx = None


# DeepSeek R1:14b needs longer to load and reason — 180s default, overridable via env
_OLLAMA_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "180"))
_CLIENT_SINGLETON = None
_CLIENT_LOCK = threading.Lock()


def _client():
    global _CLIENT_SINGLETON
    if _CLIENT_SINGLETON is not None:
        return _CLIENT_SINGLETON
    with _CLIENT_LOCK:
        if _CLIENT_SINGLETON is not None:
            return _CLIENT_SINGLETON
        if httpx is None:
            _CLIENT_SINGLETON = _ollama.Client(timeout=_OLLAMA_TIMEOUT_SECONDS)
        else:
            timeout = httpx.Timeout(connect=5.0, read=_OLLAMA_TIMEOUT_SECONDS, write=15.0, pool=5.0)
            _CLIENT_SINGLETON = _ollama.Client(timeout=timeout)
        return _CLIENT_SINGLETON


def _close_client():
    client = _CLIENT_SINGLETON
    if client is None:
        return
    transport = getattr(client, "_client", None)
    close = getattr(transport, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


atexit.register(_close_client)

def _strip_markdown(text: str) -> str:
    """Remove markdown artifacts because Jarvis responses are spoken aloud."""
    # Strip DeepSeek R1 internal thinking blocks — not for TTS
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'^\s*#{1,6}\s+', '', text, flags=re.M)
    text = re.sub(r'^\s*[-*•]\s+', '', text, flags=re.M)
    text = re.sub(r'^\s*\d+[.)]\s+', '', text, flags=re.M)
    text = re.sub(r'```\w*\n?', '', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Collapse excess blank lines left after stripping
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _is_available(model: str) -> bool:
    """Check if a model is pulled and available."""
    try:
        models = [m.model for m in _client().list().models]
        return any(model in m for m in models)
    except Exception:
        return False


def get_best_available(preferred: str) -> str:
    """Return preferred model if available, else fall back to first available."""
    try:
        models = [m.model for m in _client().list().models]
        if not models:
            raise RuntimeError("No Ollama models found. Run: ollama pull llama3.1:8b")
        if LOCAL_PREFER_TUNED and LOCAL_TUNED and any(LOCAL_TUNED in m for m in models):
            if preferred == LOCAL_DEFAULT:
                return LOCAL_TUNED
        if any(preferred in m for m in models):
            return preferred
        return models[0]
    except Exception as e:
        raise RuntimeError(f"Ollama not running. Start it with: ollama serve\n{e}")


def ask_local(user_input: str, model: str = LOCAL_DEFAULT, system_extra: str = "", track_context: bool = False) -> str:
    return "".join(ask_local_stream(user_input, model, system_extra=system_extra, track_context=track_context))


def ask_local_stream(
    user_input: str,
    model: str = LOCAL_DEFAULT,
    system_extra: str = "",
    track_context: bool = False,
    raise_on_error: bool = False,
):
    """Stream a response from a local Ollama model."""
    model = get_best_available(model)

    # Inject chain-of-thought boost for non-trivial inputs (skip for short commands)
    word_count = len(user_input.split())
    if word_count > 6 and not system_extra:
        system_extra = _REASONING_BOOST
    elif word_count > 6 and _REASONING_BOOST not in system_extra:
        system_extra = _REASONING_BOOST + "\n\n" + system_extra

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
    prompt_eval_count = None
    eval_count = None
    try:
        stream = _client().chat(
            model=model,
            messages=messages,
            stream=True
        )
        raw_buffer = ""
        for chunk in stream:
            prompt_eval_count = getattr(chunk, "prompt_eval_count", prompt_eval_count)
            eval_count = getattr(chunk, "eval_count", eval_count)
            delta = chunk.message.content or ""
            full_reply += delta
            raw_buffer += delta
            # Only yield at sentence boundaries — strip the full buffer each time
            # so multi-line patterns (think tags, bullets) are caught correctly
            if any(raw_buffer.rstrip().endswith(c) for c in ('.', '!', '?')) and len(raw_buffer) > 40:
                cleaned = _strip_markdown(raw_buffer)
                if cleaned:
                    yield cleaned
                raw_buffer = ""
        if raw_buffer:
            cleaned = _strip_markdown(raw_buffer)
            if cleaned:
                yield cleaned
    except Exception as e:
        error = (
            f"Local model error: {e}. "
            f"If Ollama is stalled, restart it with: ollama serve. "
            "You can also switch Jarvis to auto mode for cloud fallback."
        )
        if raise_on_error:
            raise RuntimeError(error) from e
        yield error
        full_reply = error

    cleaned_reply = _strip_markdown(full_reply)
    usage_tracker.record(
        provider="ollama",
        model=model,
        local=True,
        source="brain_ollama.ask_local_stream",
        prompt_tokens=prompt_eval_count,
        completion_tokens=eval_count,
        total_tokens=((prompt_eval_count or 0) + (eval_count or 0)) if (prompt_eval_count is not None or eval_count is not None) else None,
        messages=messages,
        response_text=cleaned_reply,
        estimated=(prompt_eval_count is None and eval_count is None),
        metadata={"track_context": track_context},
    )

    if track_context:
        ctx.end_turn(cleaned_reply)


def list_local_models() -> list[str]:
    """Return names of all pulled local models."""
    try:
        return [m.model for m in _client().list().models]
    except Exception:
        return []
