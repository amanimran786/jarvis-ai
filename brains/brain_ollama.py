"""
Local brain using Ollama — runs entirely on your Mac.
No API keys, no external servers, no restrictions, completely private.
"""

import ollama as _ollama
import re
import os
import atexit
import threading
import time
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
_VISION_SYSTEM_PROMPT = (
    "You are Jarvis handling local vision analysis. "
    "Describe only what is actually visible in the image. "
    "If the image is blank, unclear, low-signal, or unreadable, say that directly. "
    "Do not invent text, UI, objects, or scene details that are not supported by the pixels. "
    "Keep the answer concise and spoken-language friendly."
)

try:
    import httpx
except Exception:
    httpx = None


# DeepSeek R1:14b reasons heavily before first token — 600s default, overridable via env
_OLLAMA_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "600"))
_OLLAMA_VISION_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_VISION_TIMEOUT_SECONDS", "30"))
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


def _vision_client():
    if httpx is None:
        return _ollama.Client(timeout=_OLLAMA_VISION_TIMEOUT_SECONDS)
    timeout = httpx.Timeout(connect=5.0, read=_OLLAMA_VISION_TIMEOUT_SECONDS, write=15.0, pool=5.0)
    return _ollama.Client(timeout=timeout)


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


# ── Ollama keepalive ──────────────────────────────────────────────────────────
# Ollama unloads a model from RAM after 5 minutes of inactivity.
# Sending a zero-token "keep-alive" ping every 3 minutes prevents that eviction
# and eliminates the 20–40 second cold-reload on the next real query.

_KEEPALIVE_INTERVAL = 180  # seconds — well inside Ollama's 5-min eviction window
_keepalive_model: str | None = None
_keepalive_thread: threading.Thread | None = None
_keepalive_stop = threading.Event()


def _keepalive_loop() -> None:
    while not _keepalive_stop.wait(_KEEPALIVE_INTERVAL):
        model = _keepalive_model
        if not model:
            continue
        try:
            # keep_alive="5m" resets Ollama's internal eviction timer without generating tokens
            _client().generate(model=model, prompt="", keep_alive="5m")
        except Exception:
            pass  # Ollama may be temporarily unavailable — just try again next cycle


def start_keepalive(model: str) -> None:
    """Pin `model` in Ollama RAM. Safe to call multiple times — updates the target model."""
    global _keepalive_model, _keepalive_thread
    _keepalive_model = model
    if _keepalive_thread is not None and _keepalive_thread.is_alive():
        return
    _keepalive_stop.clear()
    _keepalive_thread = threading.Thread(
        target=_keepalive_loop,
        daemon=True,
        name="OllamaKeepalive",
    )
    _keepalive_thread.start()


def stop_keepalive() -> None:
    _keepalive_stop.set()


atexit.register(stop_keepalive)


def _strip_markdown(text: str) -> str:
    """Remove markdown artifacts because Jarvis responses are spoken aloud."""
    # Strip DeepSeek R1 internal thinking blocks — not for TTS
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'^\s*#{1,6}\s+', '', text, flags=re.M)
    text = re.sub(r'^\s*[-*•]\s+', '', text, flags=re.M)
    # Strip numbered list markers at line start — with or without trailing space
    text = re.sub(r'^\s*\d+[.)]\s*', '', text, flags=re.M)
    # Strip inline numbered list markers (e.g. "1. First 2. Second" or "1.First 2.Second")
    text = re.sub(r'(?<=\s)\d+[.)]\s*', ' ', text)
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


def ask_local(user_input: str, model: str = LOCAL_DEFAULT, system_extra: str = "", track_context: bool = False, raise_on_error: bool = False) -> str:
    return "".join(ask_local_stream(user_input, model, system_extra=system_extra, track_context=track_context, raise_on_error=raise_on_error))


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
        # Cap context for DeepSeek R1 to limit reasoning token explosion on Mac.
        # 8192 is enough for all Jarvis use cases and keeps response time manageable.
        options = {}
        if "deepseek" in model.lower():
            options["num_ctx"] = int(os.getenv("DEEPSEEK_CTX", "8192"))
            options["num_predict"] = int(os.getenv("DEEPSEEK_MAX_TOKENS", "1024"))

        stream = _client().chat(
            model=model,
            messages=messages,
            stream=True,
            options=options if options else None,
        )
        raw_buffer = ""
        in_think = False  # track DeepSeek R1 <think> blocks
        for chunk in stream:
            prompt_eval_count = getattr(chunk, "prompt_eval_count", prompt_eval_count)
            eval_count = getattr(chunk, "eval_count", eval_count)
            delta = chunk.message.content or ""
            full_reply += delta
            raw_buffer += delta

            # Track think block state to yield keepalive during long reasoning
            if "<think>" in raw_buffer and not in_think:
                in_think = True
            if "</think>" in raw_buffer and in_think:
                in_think = False
                # Think block done — strip it and flush the real answer start
                raw_buffer = re.sub(r'<think>.*?</think>', '', raw_buffer, flags=re.DOTALL)

            # During think phase yield empty string as keepalive — keeps SSE
            # connection alive while DeepSeek R1 reasons internally
            if in_think:
                yield ""
                continue

            # Outside think: yield at sentence boundaries
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
        if raise_on_error:
            raise RuntimeError(str(e)) from e
        # Voice-friendly fallback — don't expose internal restart instructions to the speaker
        error = "I wasn't able to complete that one. The local model took too long to respond. Try again or ask something simpler."
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


_LOCAL_VISION_MODEL = os.getenv("LOCAL_VISION_MODEL", "").strip()
_LOCAL_VISION_MODELS = ("llava:7b", "llava", "minicpm-v", "moondream", "llava-llama3")
_LOCAL_EMBED_MODEL = os.getenv("LOCAL_EMBED_MODEL", "nomic-embed-text")
_VISION_FAILURE_COOLDOWN_SECONDS = float(os.getenv("OLLAMA_VISION_FAILURE_COOLDOWN_SECONDS", "180"))
_vision_failures: dict[str, dict[str, float | int | str]] = {}
_vision_failures_lock = threading.Lock()


def _vision_candidates() -> list[str]:
    """Return available local vision models in preference order."""
    available = list_local_models()
    ranked: list[str] = []
    preferred = [item.strip() for item in _LOCAL_VISION_MODEL.split(",") if item.strip()]
    for candidate in [*preferred, *_LOCAL_VISION_MODELS]:
        prefix = candidate.split(":")[0]
        for model in available:
            if (model == candidate or prefix in model) and model not in ranked:
                ranked.append(model)
    return ranked


def _vision_health_snapshot() -> dict[str, dict[str, float | int | str]]:
    with _vision_failures_lock:
        return {model: data.copy() for model, data in _vision_failures.items()}


def _vision_model_on_cooldown(model: str) -> bool:
    now = time.monotonic()
    with _vision_failures_lock:
        data = _vision_failures.get(model)
        if not data:
            return False
        until = float(data.get("cooldown_until", 0.0) or 0.0)
        if until <= now:
            _vision_failures.pop(model, None)
            return False
        return True


def _mark_vision_success(model: str) -> None:
    with _vision_failures_lock:
        _vision_failures.pop(model, None)


def _mark_vision_failure(model: str, error: Exception | str) -> None:
    now = time.monotonic()
    message = str(error)
    with _vision_failures_lock:
        previous = _vision_failures.get(model, {})
        failures = int(previous.get("failures", 0) or 0) + 1
        _vision_failures[model] = {
            "failures": failures,
            "last_error": message,
            "last_failed_at": now,
            "cooldown_until": now + _VISION_FAILURE_COOLDOWN_SECONDS,
        }


def _best_vision_model() -> str | None:
    """Return the best available healthy local vision model, or None if none pulled."""
    candidates = _vision_candidates()
    for model in candidates:
        if not _vision_model_on_cooldown(model):
            return model
    return candidates[0] if candidates else None


def _best_embed_model() -> str | None:
    available = list_local_models()
    for model in available:
        if _LOCAL_EMBED_MODEL.split(":")[0] in model:
            return model
    return None


def _vision_runtime_status() -> dict[str, str | None]:
    candidates = _vision_candidates()
    healthy = [model for model in candidates if not _vision_model_on_cooldown(model)]
    health = _vision_health_snapshot()
    preferred = healthy[0] if healthy else (candidates[0] if candidates else None)

    if not candidates:
        return {
            "state": "unavailable",
            "detail": "No local vision model installed. Pull one with: ollama pull llava:7b",
            "preferred": preferred,
        }

    if healthy and not health:
        return {
            "state": "ready",
            "detail": f"Local vision ready via {healthy[0]}.",
            "preferred": healthy[0],
        }

    if healthy:
        cooled = [model for model in candidates if model not in healthy]
        detail = f"Local vision ready via {healthy[0]}."
        if cooled:
            detail += f" {cooled[0]} is cooling down after a recent failure."
        return {
            "state": "degraded",
            "detail": detail,
            "preferred": healthy[0],
        }

    cooled = candidates[0]
    return {
        "state": "degraded",
        "detail": (
            f"Local vision is installed but temporarily unhealthy. {cooled} is cooling down after a recent failure."
        ),
        "preferred": cooled,
    }


def ask_local_vision(image_path: str, prompt: str, system_extra: str = "") -> str:
    """Analyse an image with a local multimodal model (llava/minicpm-v).

    Returns the model's description, or empty string if no vision model is available.
    Reads the image from disk, encodes it as base64, and sends via the Ollama
    multimodal chat API.
    """
    candidates = _vision_candidates()
    if not candidates:
        return ""
    try:
        import base64
        with open(image_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        print(f"[Ollama Vision] failed to read image: {e}")
        return ""

    system = _VISION_SYSTEM_PROMPT
    if system_extra:
        system += "\n\n" + system_extra
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": prompt,
            "images": [image_b64],
        },
    ]

    for model in candidates:
        if _vision_model_on_cooldown(model):
            continue
        try:
            response = _vision_client().chat(model=model, messages=messages, stream=False)
            raw = (response.message.content or "").strip()
            if raw:
                _mark_vision_success(model)
                return _strip_markdown(raw)
            _mark_vision_failure(model, "empty vision response")
        except Exception as e:
            _mark_vision_failure(model, e)
            print(f"[Ollama Vision] {model} failed: {e}")
            continue
    return ""


def embed(text: str) -> list[float] | None:
    """Generate a local embedding vector via nomic-embed-text (or first available).

    Returns None if no embedding model is available.
    """
    model = _best_embed_model()
    if not model:
        return None
    try:
        response = _client().embeddings(model=model, prompt=text)
        return response.embedding
    except Exception as e:
        print(f"[Ollama Embed] failed: {e}")
        return None


def warm_model_cache(model: str = LOCAL_REASONING) -> None:
    """Pre-load a model into Ollama's GPU/RAM so the first real query is instant.

    Runs a trivial generation — discards output. Safe to call from a background
    thread at startup so it doesn't block the API from coming up.
    """
    try:
        target = get_best_available(model)
        print(f"[Ollama] Warming model cache for {target}...")
        _client().chat(
            model=target,
            messages=[{"role": "user", "content": "Hi"}],
            stream=False,
        )
        print(f"[Ollama] {target} loaded and ready.")
    except Exception as e:
        print(f"[Ollama] Cache warm failed (non-fatal): {e}")


def warm_vision_cache() -> None:
    """Pre-load the best available vision model so first image analysis is faster."""
    target = _best_vision_model()
    if not target:
        return
    try:
        print(f"[Ollama] Warming vision cache for {target}...")
        _client().generate(model=target, prompt="", keep_alive="5m")
        print(f"[Ollama] Vision model {target} loaded and ready.")
        _mark_vision_success(target)
    except Exception as e:
        _mark_vision_failure(target, e)
        print(f"[Ollama] Vision warm failed (non-fatal): {e}")


def local_capabilities() -> dict:
    models = list_local_models()
    vision_runtime = _vision_runtime_status()

    def _selected(preferred: str) -> str | None:
        if not models:
            return None
        try:
            return get_best_available(preferred)
        except Exception:
            return None

    return {
        "models": models,
        "selected_default": _selected(LOCAL_DEFAULT),
        "selected_coder": _selected(LOCAL_CODER),
        "selected_reasoning": _selected(LOCAL_REASONING),
        "vision_model": _best_vision_model(),
        "vision_preferred": _LOCAL_VISION_MODEL or None,
        "vision_candidates": _vision_candidates(),
        "vision_health": _vision_health_snapshot(),
        "vision_status": vision_runtime["state"],
        "vision_status_detail": vision_runtime["detail"],
        "vision_timeout_seconds": _OLLAMA_VISION_TIMEOUT_SECONDS,
        "embedding_model": _best_embed_model(),
        "reasoning_boost_enabled": True,
        "timeout_seconds": _OLLAMA_TIMEOUT_SECONDS,
    }
