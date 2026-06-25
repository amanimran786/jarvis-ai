"""
Local brain using Ollama — runs entirely on your Mac.
No API keys, no external servers, no restrictions, completely private.
"""

import ollama as _ollama
import json
import logging
import re
import os
import atexit
import threading
import time
import uuid
from urllib.parse import urlparse
from typing import Any, Generator
from config import SYSTEM_PROMPT, LOCAL_DEFAULT, LOCAL_CODER, LOCAL_REASONING, LOCAL_TUNED, LOCAL_PREFER_TUNED
import context_budget
import memory as mem
import conversation_context as ctx
import usage_tracker

log = logging.getLogger(__name__)

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
_OLLAMA_STRUCTURED_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_STRUCTURED_TIMEOUT_SECONDS", "20"))
_CLIENT_SINGLETON = None
_CLIENT_LOCK = threading.Lock()


def _client():
    global _CLIENT_SINGLETON
    if _CLIENT_SINGLETON is not None:
        return _CLIENT_SINGLETON
    with _CLIENT_LOCK:
        if _CLIENT_SINGLETON is not None:
            return _CLIENT_SINGLETON
        _enforce_ollama_host_policy()
        if httpx is None:
            _CLIENT_SINGLETON = _ollama.Client(timeout=_OLLAMA_TIMEOUT_SECONDS)
        else:
            timeout = httpx.Timeout(connect=5.0, read=_OLLAMA_TIMEOUT_SECONDS, write=15.0, pool=5.0)
            _CLIENT_SINGLETON = _ollama.Client(timeout=timeout)
    return _CLIENT_SINGLETON


def get_client():
    """Return the Ollama client singleton. Public accessor for task_runtime."""
    return _client()


def _ollama_host_is_local(raw: str) -> bool:
    """Accept loopback/unix Ollama endpoints; remote hosts require explicit opt-in."""
    value = (raw or "").strip()
    if not value:
        return True
    if value.startswith("unix://"):
        return True
    parsed = urlparse(value if "://" in value else f"http://{value}")
    return (parsed.hostname or "").lower() in {
        "localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal",
    }


def _ollama_endpoint_scope() -> str:
    host = os.getenv("OLLAMA_HOST", "").strip()
    if not host:
        return "on_device"
    parsed = urlparse(host if "://" in host else f"http://{host}")
    hostname = (parsed.hostname or "").lower()
    if hostname == "host.docker.internal":
        return "host_local"
    if _ollama_host_is_local(host):
        return "on_device"
    return "remote_trusted"


def _ollama_usage_is_local() -> bool:
    return _ollama_endpoint_scope() != "remote_trusted"


def _enforce_ollama_host_policy() -> None:
    host = os.getenv("OLLAMA_HOST", "").strip()
    if not host or _ollama_host_is_local(host):
        return
    allowed = os.getenv("JARVIS_ALLOW_REMOTE_OLLAMA", "").strip().lower()
    if allowed not in {"1", "true", "yes", "on"}:
        raise RuntimeError(
            "Remote OLLAMA_HOST is disabled; set JARVIS_ALLOW_REMOTE_OLLAMA=1 "
            "only for an explicitly trusted endpoint."
        )
    parsed = urlparse(host if "://" in host else f"http://{host}")
    hostname = (parsed.hostname or "").lower()
    trusted = {
        item.strip().lower()
        for item in os.getenv("JARVIS_TRUSTED_OLLAMA_HOSTS", "").split(",")
        if item.strip()
    }
    if hostname not in trusted:
        raise RuntimeError(
            "Remote OLLAMA_HOST is not in JARVIS_TRUSTED_OLLAMA_HOSTS."
        )
    insecure_allowed = os.getenv(
        "JARVIS_ALLOW_INSECURE_REMOTE_OLLAMA", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    if parsed.scheme != "https" and not insecure_allowed:
        raise RuntimeError(
            "Remote Ollama requires HTTPS unless "
            "JARVIS_ALLOW_INSECURE_REMOTE_OLLAMA=1 is explicitly set."
        )


def _vision_client():
    _enforce_ollama_host_policy()
    if httpx is None:
        return _ollama.Client(timeout=_OLLAMA_VISION_TIMEOUT_SECONDS)
    timeout = httpx.Timeout(connect=5.0, read=_OLLAMA_VISION_TIMEOUT_SECONDS, write=15.0, pool=5.0)
    return _ollama.Client(timeout=timeout)


def _structured_client():
    _enforce_ollama_host_policy()
    if httpx is None:
        return _ollama.Client(timeout=_OLLAMA_STRUCTURED_TIMEOUT_SECONDS)
    timeout = httpx.Timeout(connect=3.0, read=_OLLAMA_STRUCTURED_TIMEOUT_SECONDS, write=10.0, pool=3.0)
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
            logging.debug("[BrainOllama] silent failure in _close_client", exc_info=True)


atexit.register(_close_client)


# ── Ollama keepalive ──────────────────────────────────────────────────────────
# Ollama unloads a model from RAM after 5 minutes of inactivity.
# Sending a zero-token "keep-alive" ping every 3 minutes prevents that eviction
# and eliminates the 20–40 second cold-reload on the next real query.

_KEEPALIVE_INTERVAL = 180  # seconds — well inside Ollama's 5-min eviction window
_keepalive_model: str | None = None
_keepalive_thread: threading.Thread | None = None
_keepalive_stop = threading.Event()

# Memory pressure threshold — skip keepalive ping (allow eviction) when available
# RAM drops below this fraction. Prevents model weights from being pinned while
# the system is swapping, which causes 120s+ timeouts on every request.
_KEEPALIVE_MIN_FREE_RAM_FRACTION = float(os.getenv("JARVIS_KEEPALIVE_MIN_FREE_RAM", "0.12"))


def _system_has_headroom() -> bool:
    """Return True when there is enough free RAM to keep models pinned."""
    try:
        import subprocess
        result = subprocess.run(
            ["sysctl", "-n", "kern.memorystatus_level"],
            capture_output=True, text=True, timeout=2
        )
        # kern.memorystatus_level: 0–100, higher = more memory available
        level = int(result.stdout.strip())
        return level >= int(_KEEPALIVE_MIN_FREE_RAM_FRACTION * 100)
    except Exception:
        logging.debug("[BrainOllama] silent failure in _system_has_headroom", exc_info=True)
    try:
        import psutil
        vm = psutil.virtual_memory()
        return vm.available / vm.total >= _KEEPALIVE_MIN_FREE_RAM_FRACTION
    except Exception:
        return True  # can't check — assume ok, don't break existing behaviour


def _keepalive_loop() -> None:
    while not _keepalive_stop.wait(_KEEPALIVE_INTERVAL):
        model = _keepalive_model
        if not model:
            continue
        if not _system_has_headroom():
            # RAM is tight — let Ollama evict the model naturally rather than
            # pinging to keep it loaded. A cold reload (~20s) is far better
            # than a 120s timeout caused by swapping under memory pressure.
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
    text = re.sub(r'(?is)\bThinking\.\.\..*?\.\.\.done thinking\.\s*', '', text)
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


# Approximate effective context windows of locally-installed models. The
# values are deliberately conservative — we route by 80% of the limit so the
# model never gets an over-filled prompt. Override at runtime via env if you
# pull a model with a larger context (e.g. `LOCAL_MODEL_CTX_qwen3-coder=...`).
_LOCAL_MODEL_CONTEXT_TOKENS = {
    # Upstream supports 1M; Jarvis intentionally serves/evaluates at 64K until
    # endpoint-specific memory and long-context soak tests pass.
    "glm-5.2": 64000,
    "glm-4.7-flash": 202752,
    "gemma4:e4b": 8192,
    "gemma3:4b": 8192,
    "llama3.1:8b": 8192,
    "qwen3:8b": 32768,
    "qwen3:14b": 131072,
    "qwen3:30b": 131072,
    "qwen2.5:32b": 131072,
    "qwen2.5-coder:7b": 32768,
    "qwen2.5-coder:32b": 131072,
    "devstral": 32768,
    "phi4-mini": 4096,
    "phi4": 16384,
    "deepseek-r1:14b": 8192,  # we cap DeepSeek to 8k via DEEPSEEK_CTX anyway
    "qwen3-coder:30b": 262144,
    "qwen3.6:35b": 262144,
}

# Escalation order when the requested model can't fit the prompt.
_LOCAL_FALLBACK_ORDER = ("glm-4.7-flash", "qwen3:8b")


def _model_context_limit(model: str) -> int:
    for known, limit in _LOCAL_MODEL_CONTEXT_TOKENS.items():
        if known in model:
            return limit
    return 8192


def _fits_local(prompt: str, model: str) -> str:
    """Return a model that can fit the prompt, escalating only when needed.

    Token estimate is the cheap chars/4 heuristic — good enough to catch the
    cliff between a 7B (8k) and qwen3-coder:30b (262k). We require an 80%
    headroom so generation has room to grow.
    """
    if not prompt:
        return model
    estimated_tokens = max(1, len(prompt) // 4)
    limit = _model_context_limit(model)
    if estimated_tokens < int(limit * 0.8):
        return model
    for candidate in _LOCAL_FALLBACK_ORDER:
        cand_limit = _model_context_limit(candidate)
        if estimated_tokens < int(cand_limit * 0.8):
            print(f"[Ollama] Escalating from {model} to {candidate} for prompt fit "
                  f"(~{estimated_tokens} tokens > {int(limit * 0.8)} headroom).")
            return candidate
    return model


def _messages_text(messages: list[dict]) -> str:
    return "\n\n".join(str(m.get("content") or "") for m in messages or [])


def _cap_track_context_messages(
    messages: list[dict],
    *,
    target_tokens: int | None = None,
    reserve_response_tokens: int = 1024,
) -> tuple[list[dict], dict[str, Any]]:
    """Drop oldest active conversation messages before sending to Ollama.

    The in-memory conversation state is untouched. We only reduce the prompt
    payload for this provider call, preserving the system message and current
    user request so Jarvis stays accurate under a local context budget.
    """
    target = int(target_tokens or context_budget.target_tokens_for("chat"))
    prompt_budget = max(1, target - int(reserve_response_tokens))
    original_tokens = context_budget.estimate_tokens(_messages_text(messages))
    report: dict[str, Any] = {
        "target_tokens": target,
        "reserve_response_tokens": int(reserve_response_tokens),
        "prompt_budget_tokens": prompt_budget,
        "original_prompt_tokens": original_tokens,
        "final_prompt_tokens": original_tokens,
        "dropped_message_count": 0,
        "dropped_message_tokens": 0,
        "dropped_messages": [],
        "over_budget": original_tokens > prompt_budget,
    }
    if original_tokens <= prompt_budget or len(messages or []) <= 2:
        return messages, report

    capped = list(messages)
    dropped_messages: list[dict[str, Any]] = []
    dropped_tokens = 0
    while len(capped) > 2 and context_budget.estimate_tokens(_messages_text(capped)) > prompt_budget:
        dropped = capped.pop(1)
        content = str(dropped.get("content") or "")
        tokens = context_budget.estimate_tokens(content)
        dropped_tokens += tokens
        dropped_messages.append({
            "role": str(dropped.get("role") or "unknown"),
            "tokens": tokens,
        })

    final_tokens = context_budget.estimate_tokens(_messages_text(capped))
    report.update({
        "final_prompt_tokens": final_tokens,
        "dropped_message_count": len(dropped_messages),
        "dropped_message_tokens": dropped_tokens,
        "dropped_messages": dropped_messages,
        "over_budget": final_tokens > prompt_budget,
    })
    return capped, report


def _ollama_options_for_model(model: str) -> dict[str, int]:
    """Model-specific runtime options that keep local context explicit."""
    lower = (model or "").lower()
    options: dict[str, int] = {}
    if "glm" in lower:
        # GLM 4.7 Flash supports 202K context; 128K is practical for M4 Pro 48 GB.
        # Override via GLM_CTX env if you want a different value.
        options["num_ctx"] = int(os.getenv("GLM_CTX", os.getenv("OLLAMA_GLM_CONTEXT", "131072")))
    if "deepseek" in lower:
        # Cap DeepSeek R1 to limit reasoning token explosion on Mac.
        options["num_ctx"] = int(os.getenv("DEEPSEEK_CTX", "8192"))
        options["num_predict"] = int(os.getenv("DEEPSEEK_MAX_TOKENS", "1024"))
    if "qwen3" in lower:
        if any(tag in lower for tag in ("30b", "32b", "14b")):
            # Larger Qwen3 variants: 128K practical window on 48 GB
            options.setdefault("num_ctx", int(os.getenv("QWEN3_LARGE_CTX", "131072")))
        else:
            # qwen3:8b and smaller: native 32K window
            options.setdefault("num_ctx", int(os.getenv("QWEN3_CTX", "32768")))
    if "devstral" in lower:
        options.setdefault("num_ctx", int(os.getenv("DEVSTRAL_CTX", "32768")))
    return options


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
        if _is_cloud_tagged_model(preferred):
            raise RuntimeError(f"Cloud-tagged model is not allowed in local runtime: {preferred}")
        models = [
            m.model for m in _client().list().models
            if not _is_cloud_tagged_model(m.model)
        ]
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


def _is_cloud_tagged_model(model: str) -> bool:
    lower = (model or "").strip().lower()
    name, _, tag = lower.partition(":")
    # 'cloud/' as a slash-delimited namespace prefix marks a remote-only model.
    # Hyphen-joined names like 'cloud-native' are local models and must not match.
    if re.match(r"cloud/", name):
        return True
    # Version tag (after ':') containing 'cloud' as a word marks remote-only.
    if not tag:
        return False
    return bool(re.search(r"(?:^|[-/_])cloud(?:$|[-/_])", tag))


def _normalize_model_tag(model: str) -> str:
    value = (model or "").strip()
    return value[:-7] if value.endswith(":latest") else value


def _exact_available_model(model: str) -> str:
    requested = _normalize_model_tag(model)
    if not requested or _is_cloud_tagged_model(requested):
        raise RuntimeError("A non-cloud exact local model is required.")
    models = [
        item.model for item in _client().list().models
        if not _is_cloud_tagged_model(item.model)
    ]
    for available in models:
        if _normalize_model_tag(available) == requested:
            return available
    raise RuntimeError(f"Exact local model is unavailable: {model}")


def ask_local(
    user_input: str,
    model: str = LOCAL_DEFAULT,
    system_extra: str = "",
    track_context: bool = False,
    raise_on_error: bool = False,
    strict_model: bool = False,
    include_memory: bool = True,
) -> str:
    return "".join(ask_local_stream(
        user_input,
        model,
        system_extra=system_extra,
        track_context=track_context,
        raise_on_error=raise_on_error,
        strict_model=strict_model,
        include_memory=include_memory,
    ))


def ask_local_structured(
    user_input: str,
    schema: dict[str, Any] | str,
    model: str = LOCAL_DEFAULT,
    system: str = "",
    raise_on_error: bool = True,
) -> str:
    """Return one non-streamed local response constrained by an Ollama JSON schema."""
    prompt_for_fit = f"{system}\n\n{user_input}" if system else user_input
    model = _fits_local(prompt_for_fit, model)
    model = get_best_available(model)

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user_input})

    try:
        response = _structured_client().chat(
            model=model,
            messages=messages,
            stream=False,
            format=schema,
            options={
                "temperature": 0,
                "num_predict": int(os.getenv("OLLAMA_STRUCTURED_MAX_TOKENS", "256")),
            },
        )
        content = (response.message.content or "").strip()
        prompt_eval_count = getattr(response, "prompt_eval_count", None)
        eval_count = getattr(response, "eval_count", None)
        usage_tracker.record(
            provider="ollama",
            model=model,
            local=_ollama_usage_is_local(),
            source="brain_ollama.ask_local_structured",
            prompt_tokens=prompt_eval_count,
            completion_tokens=eval_count,
            total_tokens=((prompt_eval_count or 0) + (eval_count or 0)) if (prompt_eval_count is not None or eval_count is not None) else None,
            messages=messages,
            response_text=content,
            estimated=(prompt_eval_count is None and eval_count is None),
            metadata={"structured": True, "endpoint_scope": _ollama_endpoint_scope()},
        )
        return content
    except Exception as e:
        if raise_on_error:
            raise RuntimeError(str(e)) from e
        return ""


def _prune_prompt(user_input: str, system_base: str, system_extra: str = "") -> str:
    """Prune the SYSTEM_PROMPT dynamically to save tokens based on query intent."""
    user_lower = user_input.lower()
    extra_lower = (system_extra or "").lower()
    
    # Message compose keywords
    compose_keywords = {"compose", "write an email", "send message", "text", "imessage", "email", "reply to", "draft", "message"}
    is_compose = any(kw in user_lower or kw in extra_lower for kw in compose_keywords)
    
    # Coding / terminal console keywords
    code_keywords = {"code", "run", "test", "git", "install", "cli", "terminal", "python", "file", "fix", "directory", "test suite", "sh", "bash", "compile", "script", "repo", "diff", "patch"}
    is_code = any(kw in user_lower or kw in extra_lower for kw in code_keywords)
    
    pruned = system_base
    
    # Section 1: Voice output rules
    if is_code:
        pruned = re.sub(
            r"Voice output rules — TTS reads every response aloud:.*?(?=Honesty rules:)",
            "",
            pruned,
            flags=re.DOTALL
        )
        
    # Section 2: Terminal console rules
    if not is_code:
        pruned = re.sub(
            r"Terminal console:.*?(?=Message composition:)",
            "",
            pruned,
            flags=re.DOTALL
        )
        
    # Section 3: Message composition rules
    if not is_compose:
        pruned = re.sub(
            r"Message composition:.*$",
            "",
            pruned,
            flags=re.DOTALL
        )
        
    # Clean up double newlines
    pruned = re.sub(r"\n{3,}", "\n\n", pruned).strip()
    return pruned


def ask_local_stream(
    user_input: str,
    model: str = LOCAL_DEFAULT,
    system_extra: str = "",
    track_context: bool = False,
    raise_on_error: bool = False,
    context_budget_report: dict[str, Any] | None = None,
    strict_model: bool = False,
    include_memory: bool = True,
):
    """Stream a response from a local Ollama model."""
    # Inject chain-of-thought boost only for task/question inputs, not casual conversation.
    # Casual statements lack a question mark and don't contain task/technical keywords — injecting
    # the reasoning prompt on those makes small models respond with "please clarify the question."
    word_count = len(user_input.split())
    _is_question = "?" in user_input
    _task_keywords = (
        "how", "why", "what", "when", "where", "which", "who",
        "can you", "could you", "please", "help", "write", "create",
        "fix", "debug", "run", "open", "send", "search", "find",
        "check", "show", "list", "explain", "compare", "analyze",
        "code", "script", "file", "test", "build", "deploy",
    )
    _lower_input = user_input.lower()
    _is_task = any(kw in _lower_input for kw in _task_keywords)
    _needs_boost = word_count > 6 and (_is_question or _is_task)
    if _needs_boost and not system_extra:
        system_extra = _REASONING_BOOST
    elif _needs_boost and _REASONING_BOOST not in system_extra:
        system_extra = _REASONING_BOOST + "\n\n" + system_extra

    pruned_prompt = _prune_prompt(user_input, SYSTEM_PROMPT, system_extra)
    system_base = pruned_prompt + (mem.get_context() if include_memory else "")
    if track_context:
        ctx.begin_turn(user_input)
        system, messages, _ = ctx.build_prompt_state(system_base, system_extra=system_extra)
        messages = [{"role": "system", "content": system}] + messages
        messages, conversation_budget_report = _cap_track_context_messages(
            messages,
            target_tokens=context_budget.target_tokens_for("chat", model=model, local=True),
        )
    else:
        system = system_base
        if system_extra:
            system += "\n\n" + system_extra
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user_input}]
        conversation_budget_report = None

    # Escalate to a larger-context local model only after the full prompt is
    # assembled. User input alone hides the real cost of system + memory blocks.
    fitted_model = _fits_local(_messages_text(messages), model)
    if strict_model:
        if _normalize_model_tag(fitted_model) != _normalize_model_tag(model):
            raise RuntimeError(f"Exact model cannot fit the assembled prompt: {model}")
        model = _exact_available_model(model)
    else:
        model = get_best_available(fitted_model)

    full_reply = ""
    prompt_eval_count = None
    eval_count = None
    try:
        options = _ollama_options_for_model(model)

        stream = _client().chat(
            model=model,
            messages=messages,
            stream=True,
            options=options if options else None,
        )
        raw_buffer = ""
        in_think = False  # track local reasoning blocks
        for chunk in stream:
            prompt_eval_count = getattr(chunk, "prompt_eval_count", prompt_eval_count)
            eval_count = getattr(chunk, "eval_count", eval_count)
            delta = chunk.message.content or ""
            full_reply += delta
            raw_buffer += delta

            # Track think block state to yield keepalive during long reasoning.
            if ("<think>" in raw_buffer or "Thinking..." in raw_buffer) and not in_think:
                in_think = True
            if ("</think>" in raw_buffer or "...done thinking." in raw_buffer) and in_think:
                in_think = False
                # Think block done; strip it and flush the real answer start.
                raw_buffer = re.sub(r'<think>.*?</think>', '', raw_buffer, flags=re.DOTALL)
                raw_buffer = re.sub(r'(?is)\bThinking\.\.\..*?\.\.\.done thinking\.\s*', '', raw_buffer)

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
        local=_ollama_usage_is_local(),
        source="brain_ollama.ask_local_stream",
        prompt_tokens=prompt_eval_count,
        completion_tokens=eval_count,
        total_tokens=((prompt_eval_count or 0) + (eval_count or 0)) if (prompt_eval_count is not None or eval_count is not None) else None,
        messages=messages,
        response_text=cleaned_reply,
        estimated=(prompt_eval_count is None and eval_count is None),
        metadata={
            "track_context": track_context,
            "endpoint_scope": _ollama_endpoint_scope(),
            **({"context_budget": context_budget_report} if context_budget_report else {}),
            **({"conversation_budget": conversation_budget_report} if conversation_budget_report else {}),
        },
    )

    if track_context:
        ctx.end_turn(cleaned_reply)


def list_local_models() -> list[str]:
    """Return names of all pulled local models."""
    try:
        return [
            m.model for m in _client().list().models
            if not _is_cloud_tagged_model(m.model)
        ]
    except Exception:
        return []


# ── Agentic tool-calling loop ──────────────────────────────────────────────────

_AGENT_TOOL_SCHEMAS: dict[str, dict] = {
    "web_search": {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current information, news, documentation, or facts.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query"}},
                "required": ["query"],
            },
        },
    },
    "read_file": {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a local file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File path to read"}},
                "required": ["path"],
            },
        },
    },
    "write_file": {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write or overwrite content to a file inside the workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "File path relative to the workspace directory"},
                    "content": {"type": "string", "description": "Content to write to the file"}
                },
                "required": ["filepath", "content"],
            },
        },
    },
    "run_tests": {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Execute pytest securely within the workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pytest_args": {"type": "string", "description": "Arguments to pass to pytest"}
                },
                "required": [],
            },
        },
    },
    "get_weather": {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a city or location.",
            "parameters": {
                "type": "object",
                "properties": {"location": {"type": "string", "description": "City or location name"}},
                "required": ["location"],
            },
        },
    },
    "memory_lookup": {
        "type": "function",
        "function": {
            "name": "memory_lookup",
            "description": "Look up facts and context from Jarvis long-term memory.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "What to recall from memory"}},
                "required": ["query"],
            },
        },
    },
}

_JARVIS_ROOT = None  # pathlib.Path set lazily in _jarvis_root()


def _jarvis_root() -> "Any":
    global _JARVIS_ROOT
    if _JARVIS_ROOT is None:
        from pathlib import Path
        _JARVIS_ROOT = Path(__file__).resolve().parent.parent
    return _JARVIS_ROOT


def _execute_agent_tool(
    name: str,
    args: dict,
    *,
    workspace_confined: bool | None = None,
) -> str:
    try:
        if name == "web_search":
            from tools import web_search
            query = str(args.get("query", "")).strip()
            return web_search(query, max_results=5, summarise=False) if query else "No query provided."

        elif name == "read_file":
            raw = str(args.get("path", args.get("filepath", ""))).strip()
            if not raw:
                return "No path provided."
            root = _jarvis_root()
            confined = (
                os.getenv("JARVIS_WORKSPACE_CONFINED") == "1"
                if workspace_confined is None else workspace_confined
            )
            if confined:
                root = (root / "workspace").resolve()
            candidate = (root / raw).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                return "Access denied: path outside project root."
            if not candidate.is_file():
                return f"File not found: {raw}"
            with candidate.open("rb") as handle:
                content = handle.read(32_001)
            suffix = "\n[... file read truncated ...]" if len(content) > 32_000 else ""
            return content[:32_000].decode("utf-8", errors="replace") + suffix

        elif name == "write_file":
            from tools import write_file
            filepath = str(args.get("filepath", args.get("path", ""))).strip()
            content = str(args.get("content", ""))
            return write_file(filepath, content)

        elif name == "run_tests":
            from tools import run_tests
            pytest_args = str(args.get("pytest_args", "")).strip()
            return run_tests(pytest_args)

        elif name == "get_weather":
            from tools import get_weather
            return get_weather(str(args.get("location", "")))

        elif name == "memory_lookup":
            context = mem.get_context()
            return context if context else "No relevant memory found."

        return f"Unknown tool: {name}"
    except Exception as exc:
        return f"Tool error ({name}): {exc}"


_TOOL_RESULT_MAX_CHARS = 8_000
_TOOL_RESULT_OLDER_CHARS = 1_200
_TOOL_LOOP_RESPONSE_RESERVE = 2_048
_TOOL_LOOP_MAX_ITERATIONS = 6
_TOOL_LOOP_MAX_CALLS = 12
_TOOL_LOOP_MAX_CALLS_PER_RESPONSE = 4
_TOOL_LOOP_MAX_ARGUMENT_BYTES = 8_192
_TOOL_LOOP_MAX_OUTPUT_CHARS = 20_000
_NETWORK_AGENT_TOOLS = frozenset({"web_search", "get_weather"})
_OUTBOUND_SECRET_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9]{10,}|AIza[A-Za-z0-9_-]{20,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|(?:password|token|secret)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)


def _truncate_tool_result(text: str, limit: int = _TOOL_RESULT_MAX_CHARS) -> tuple[str, int]:
    """Bound untrusted tool output while preserving useful head and error tail."""
    value = str(text or "")
    if len(value) <= limit:
        return value, 0
    marker = "\n[... tool output truncated ...]\n"
    available = max(0, limit - len(marker))
    head = available // 3
    tail = available - head
    compact = value[:head] + marker + (value[-tail:] if tail else "")
    return compact, len(value) - len(compact)


def _serialized_prompt_tokens(messages: list[dict], tool_schemas: list[dict]) -> int:
    payload = json.dumps(
        {"messages": messages, "tools": tool_schemas},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return context_budget.estimate_tokens(payload)


def _tool_round_ranges(messages: list[dict]) -> list[list[int]]:
    rounds: list[list[int]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            round_indexes = [index]
            cursor = index + 1
            while cursor < len(messages) and messages[cursor].get("role") == "tool":
                round_indexes.append(cursor)
                cursor += 1
            if len(round_indexes) > 1:
                rounds.append(round_indexes)
            index = cursor
            continue
        index += 1
    return rounds


def _compact_tool_loop_messages(
    messages: list[dict],
    tool_schemas: list[dict],
    *,
    target_tokens: int,
    reserve_response_tokens: int = _TOOL_LOOP_RESPONSE_RESERVE,
) -> tuple[list[dict], dict[str, Any]]:
    """Compact only complete older tool rounds; preserve system/user/latest evidence."""
    capped = [dict(message) for message in messages]
    original_tokens = _serialized_prompt_tokens(capped, tool_schemas)
    prompt_budget = max(1, int(target_tokens) - int(reserve_response_tokens))
    truncated_messages = 0
    truncated_chars = 0

    rounds = _tool_round_ranges(capped)
    latest_indexes = set(rounds[-1]) if rounds else set()
    for index, message in enumerate(capped):
        if message.get("role") != "tool":
            continue
        limit = _TOOL_RESULT_MAX_CHARS if index in latest_indexes else _TOOL_RESULT_OLDER_CHARS
        content, removed = _truncate_tool_result(str(message.get("content") or ""), limit)
        if removed:
            message["content"] = content
            truncated_messages += 1
            truncated_chars += removed

    dropped_rounds = 0
    dropped_messages = 0
    while _serialized_prompt_tokens(capped, tool_schemas) > prompt_budget:
        rounds = _tool_round_ranges(capped)
        if len(rounds) <= 1:
            break
        remove_indexes = set(rounds[0])
        dropped_rounds += 1
        dropped_messages += len(remove_indexes)
        capped = [m for i, m in enumerate(capped) if i not in remove_indexes]

    final_tokens = _serialized_prompt_tokens(capped, tool_schemas)
    return capped, {
        "target_tokens": int(target_tokens),
        "reserve_response_tokens": int(reserve_response_tokens),
        "prompt_budget_tokens": prompt_budget,
        "original_prompt_tokens": original_tokens,
        "final_prompt_tokens": final_tokens,
        "within_budget": final_tokens <= prompt_budget,
        "over_budget": final_tokens > prompt_budget,
        "truncated_tool_message_count": truncated_messages,
        "truncated_tool_chars": truncated_chars,
        "dropped_tool_round_count": dropped_rounds,
        "dropped_message_count": dropped_messages,
        "dropped_estimated_tokens": max(0, original_tokens - final_tokens),
    }


def _tool_call_signature(name: str, arguments: Any) -> str:
    return json.dumps(
        {"name": str(name or ""), "arguments": arguments},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )


def _safe_outbound_query(query: str, *, sensitive_context_seen: bool) -> bool:
    return bool(query.strip()) and not sensitive_context_seen and not _OUTBOUND_SECRET_RE.search(query)


def _network_agent_tools_enabled() -> bool:
    return os.getenv("JARVIS_ALLOW_NETWORK_AGENT_TOOLS", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def ask_local_with_tools(
    user_input: str,
    model: str = LOCAL_DEFAULT,
    system_extra: str = "",
    tools: "list[str] | None" = None,  # type: ignore[type-arg]
    max_iterations: int = 6,
    workspace_confined: bool | None = None,
):
    """Agentic function-calling loop for local inference.

    Model calls tools (web_search, read_file, get_weather, memory_lookup),
    gets results, then streams the final synthesized answer — same pattern as
    Claude/GPT tool use, fully local via Ollama.

    Falls back to plain ask_local_stream when no callable tools are requested.
    """
    requested_tool_names = [t for t in (tools or []) if t in _AGENT_TOOL_SCHEMAS]
    tool_names = list(requested_tool_names)
    if not _network_agent_tools_enabled():
        tool_names = [name for name in tool_names if name not in _NETWORK_AGENT_TOOLS]
        if len(tool_names) != len(requested_tool_names):
            log.info(
                "network agent tools disabled; set JARVIS_ALLOW_NETWORK_AGENT_TOOLS=1 "
                "for explicit outbound access"
            )
    if not tool_names:
        yield from ask_local_stream(user_input, model=model, system_extra=system_extra)
        return

    tool_schemas = [_AGENT_TOOL_SCHEMAS[t] for t in tool_names]
    # Network-capable agents do not receive long-term memory by default. This
    # prevents a model from copying private memory into a generated search query.
    network_tools_enabled = bool(_NETWORK_AGENT_TOOLS.intersection(tool_names))
    system = SYSTEM_PROMPT + ("" if network_tools_enabled else mem.get_context())
    if system_extra:
        system += "\n\n" + system_extra

    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_input},
    ]
    model = get_best_available(_fits_local(_messages_text(messages), model))
    options = _ollama_options_for_model(model)
    target_tokens = context_budget.target_tokens_for("agent", model=model, local=True)
    options.setdefault("num_ctx", int(target_tokens))
    if options.get("num_ctx"):
        target_tokens = min(target_tokens, int(options["num_ctx"]))
    iterations = max(1, min(int(max_iterations), _TOOL_LOOP_MAX_ITERATIONS))
    invocation_id = uuid.uuid4().hex[:12]
    call_index = 0
    total_tool_calls = 0
    last_tool_signature: str | None = None
    sensitive_context_seen = bool(
        _OUTBOUND_SECRET_RE.search(f"{user_input}\n{system_extra}")
    )
    confined_for_invocation = (
        os.getenv("JARVIS_WORKSPACE_CONFINED") == "1"
        if workspace_confined is None else bool(workspace_confined)
    )

    def _record_call(
        *,
        sent_messages: list[dict],
        response: Any,
        response_text: str,
        phase: str,
        iteration: int | None,
        tool_call_names: list[str],
        exit_reason: str | None,
        budget_report: dict[str, Any],
    ) -> None:
        nonlocal call_index
        call_index += 1
        prompt_count = getattr(response, "prompt_eval_count", None) if response is not None else None
        eval_count = getattr(response, "eval_count", None) if response is not None else None
        call_type = "synthesis" if phase == "final_synthesis" else "decision"
        truncated = bool(
            budget_report.get("truncated_tool_message_count")
            or budget_report.get("dropped_tool_round_count")
        )
        try:
            usage_tracker.record(
                provider="ollama",
                model=model,
                local=_ollama_usage_is_local(),
                source="brain_ollama.ask_local_with_tools",
                prompt_tokens=prompt_count,
                completion_tokens=eval_count,
                total_tokens=(
                    prompt_count + eval_count
                    if prompt_count is not None and eval_count is not None else None
                ),
                messages=sent_messages,
                response_text=response_text,
                estimated=(prompt_count is None or eval_count is None),
                metadata={
                    "tool_loop": {
                        "invocation_id": invocation_id,
                        "call_index": call_index,
                        "phase": phase,
                        "call_type": call_type,
                        "iteration": iteration,
                        "max_iterations": iterations,
                        "requested_tools": list(tool_names),
                        "tool_call_count": len(tool_call_names),
                        "tool_names": tool_call_names,
                        "exit_reason": exit_reason,
                        "governor_eligible": True,
                        "governor_applied": True,
                        "truncated": truncated,
                        "dropped_tool_round_count": budget_report.get(
                            "dropped_tool_round_count", 0
                        ),
                        "dropped_message_count": budget_report.get(
                            "dropped_message_count", 0
                        ),
                        "dropped_estimated_tokens": budget_report.get(
                            "dropped_estimated_tokens", 0
                        ),
                        "max_iteration_exhausted": exit_reason == "max_iterations",
                        "error": exit_reason == "provider_error",
                        "governor": {"applied": True, **budget_report},
                        "truncation": {
                            "applied": truncated,
                            "truncated_tool_message_count": budget_report.get(
                                "truncated_tool_message_count", 0
                            ),
                            "truncated_tool_chars": budget_report.get(
                                "truncated_tool_chars", 0
                            ),
                            "dropped_tool_round_count": budget_report.get(
                                "dropped_tool_round_count", 0
                            ),
                            "dropped_message_count": budget_report.get(
                                "dropped_message_count", 0
                            ),
                            "dropped_estimated_tokens": budget_report.get(
                                "dropped_estimated_tokens", 0
                            ),
                        },
                    },
                    "endpoint_scope": _ollama_endpoint_scope(),
                },
            )
        except Exception:
            log.warning("local tool usage telemetry failed", exc_info=True)

    synthesis_reason: str | None = None
    for iteration in range(1, iterations + 1):
        sent_messages, budget_report = _compact_tool_loop_messages(
            messages, tool_schemas, target_tokens=target_tokens,
        )
        messages = sent_messages
        if budget_report["over_budget"]:
            log.warning(
                "local tool prompt rejected above context budget: %s > %s tokens",
                budget_report["final_prompt_tokens"],
                budget_report["prompt_budget_tokens"],
            )
            yield "The local tool request is too large for its safe context budget."
            return
        try:
            response = _client().chat(
                model=model,
                messages=sent_messages,
                tools=tool_schemas,
                stream=False,
                options=options or None,
            )
            last_msg = getattr(response, "message", None)
            if last_msg is None:
                raise RuntimeError("Ollama returned no message")
            raw_calls = list(getattr(last_msg, "tool_calls", None) or [])
        except Exception:
            log.exception("local tool decision failed")
            _record_call(
                sent_messages=sent_messages,
                response=None,
                response_text="",
                phase="tool_decision",
                iteration=iteration,
                tool_call_names=[],
                exit_reason="provider_error",
                budget_report=budget_report,
            )
            yield "The local tool agent could not complete this request."
            return

        if not raw_calls:
            final_text = _strip_markdown(str(getattr(last_msg, "content", "") or ""))
            _record_call(
                sent_messages=sent_messages,
                response=response,
                response_text=final_text,
                phase="tool_decision",
                iteration=iteration,
                tool_call_names=[],
                exit_reason="model_answer",
                budget_report=budget_report,
            )
            if final_text:
                yield final_text
            else:
                yield "The local tool agent returned no final answer."
            return

        accepted: list[tuple[str, dict, str]] = []
        response_signatures: set[str] = set()
        for tc in raw_calls[:_TOOL_LOOP_MAX_CALLS_PER_RESPONSE]:
            function = getattr(tc, "function", None)
            name = str(getattr(function, "name", "") or "")
            arguments = getattr(function, "arguments", {})
            if name not in tool_names or not isinstance(arguments, dict):
                continue
            signature = _tool_call_signature(name, arguments)
            if signature == last_tool_signature or signature in response_signatures:
                continue
            if len(signature.encode("utf-8", errors="replace")) > _TOOL_LOOP_MAX_ARGUMENT_BYTES:
                continue
            if total_tool_calls + len(accepted) >= _TOOL_LOOP_MAX_CALLS:
                break
            accepted.append((name, arguments, signature))
            response_signatures.add(signature)

        accepted_names = [name for name, _, _ in accepted]
        _record_call(
            sent_messages=sent_messages,
            response=response,
            response_text=str(getattr(last_msg, "content", "") or ""),
            phase="tool_decision",
            iteration=iteration,
            tool_call_names=accepted_names,
            exit_reason=None,
            budget_report=budget_report,
        )
        if not accepted:
            if total_tool_calls >= _TOOL_LOOP_MAX_CALLS:
                synthesis_reason = "tool_call_limit"
                break
            yield "The local tool agent requested invalid or repeated tool calls."
            return

        messages.append({
            "role": "assistant",
            "content": str(getattr(last_msg, "content", "") or ""),
            "tool_calls": [
                {"function": {"name": name, "arguments": arguments}}
                for name, arguments, _ in accepted
            ],
        })
        for name, arguments, signature in accepted:
            last_tool_signature = signature
            total_tool_calls += 1
            if name in _NETWORK_AGENT_TOOLS:
                query = json.dumps(arguments, ensure_ascii=False, default=str)
                if not _safe_outbound_query(
                    query, sensitive_context_seen=sensitive_context_seen
                ):
                    result = f"{name} blocked by local data-loss policy."
                else:
                    result = _execute_agent_tool(
                        name, arguments, workspace_confined=confined_for_invocation
                    )
            else:
                result = _execute_agent_tool(
                    name, arguments, workspace_confined=confined_for_invocation
                )
            if name in {"read_file", "memory_lookup"}:
                sensitive_context_seen = True
            result, _removed = _truncate_tool_result(result)
            messages.append({"role": "tool", "content": result})
        if total_tool_calls >= _TOOL_LOOP_MAX_CALLS:
            synthesis_reason = "tool_call_limit"
            break

    # Every decision round requested tools. Preserve the latest bounded evidence
    # and make one final synthesis call without exposing another tool budget.
    messages.append({
        "role": "user",
        "content": "Summarize your findings using only the tool results above.",
    })
    sent_messages, budget_report = _compact_tool_loop_messages(
        messages, tool_schemas, target_tokens=target_tokens,
    )
    if budget_report["over_budget"]:
        log.warning(
            "local tool synthesis rejected above context budget: %s > %s tokens",
            budget_report["final_prompt_tokens"],
            budget_report["prompt_budget_tokens"],
        )
        yield "The local tool results are too large for a safe final summary."
        return
    full_reply = ""
    response_for_usage = None
    exit_reason = synthesis_reason or "max_iterations"
    try:
        stream = _client().chat(
            model=model,
            messages=sent_messages,
            stream=True,
            options=options or None,
        )
        raw_buffer = ""
        for chunk in stream:
            response_for_usage = chunk
            delta = str(getattr(getattr(chunk, "message", None), "content", "") or "")
            remaining = _TOOL_LOOP_MAX_OUTPUT_CHARS - len(full_reply)
            if remaining <= 0:
                break
            delta = delta[:remaining]
            full_reply += delta
            raw_buffer += delta
            if (any(raw_buffer.rstrip().endswith(c) for c in ('.', '!', '?'))
                    and len(raw_buffer) > 40):
                cleaned = _strip_markdown(raw_buffer)
                if cleaned:
                    yield cleaned
                raw_buffer = ""
        if raw_buffer:
            cleaned = _strip_markdown(raw_buffer)
            if cleaned:
                yield cleaned
    except Exception:
        exit_reason = "provider_error"
        log.exception("local tool synthesis failed")
        if not full_reply:
            yield "The local tool agent could not finish its summary."
    finally:
        _record_call(
            sent_messages=sent_messages,
            response=response_for_usage,
            response_text=_strip_markdown(full_reply),
            phase="final_synthesis",
            iteration=None,
            tool_call_names=[],
            exit_reason=exit_reason,
            budget_report=budget_report,
        )


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
    quiet = os.getenv("JARVIS_QUIET_BOOT", "").lower() in {"1", "true", "yes"}
    try:
        target = get_best_available(model)
        if not quiet:
            print(f"[Ollama] Warming model cache for {target}...")
        _client().chat(
            model=target,
            messages=[{"role": "user", "content": "Hi"}],
            stream=False,
        )
        if not quiet:
            print(f"[Ollama] {target} loaded and ready.")
    except Exception as e:
        if not quiet:
            print(f"[Ollama] Cache warm failed (non-fatal): {e}")


def warm_vision_cache() -> None:
    """Pre-load the best available vision model so first image analysis is faster."""
    target = _best_vision_model()
    if not target:
        return
    quiet = os.getenv("JARVIS_QUIET_BOOT", "").lower() in {"1", "true", "yes"}
    try:
        if not quiet:
            print(f"[Ollama] Warming vision cache for {target}...")
        _client().generate(model=target, prompt="", keep_alive="5m")
        if not quiet:
            print(f"[Ollama] Vision model {target} loaded and ready.")
        _mark_vision_success(target)
    except Exception as e:
        _mark_vision_failure(target, e)
        if not quiet:
            print(f"[Ollama] Vision warm failed (non-fatal): {e}")


def local_capabilities() -> dict:
    models = list_local_models()
    vision_runtime = _vision_runtime_status()
    try:
        from brains import brain_apple_foundation
        apple_foundation = brain_apple_foundation.status()
    except Exception:
        apple_foundation = {"enabled": False, "available": False}

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
        "apple_foundation": apple_foundation,
        "reasoning_boost_enabled": True,
        "timeout_seconds": _OLLAMA_TIMEOUT_SECONDS,
    }


# ── Ollama Cloud (api.ollama.com) ─────────────────────────────────────────────

def ask_ollama_cloud_stream(
    user_input: str,
    model: str = "",
    *,
    system_extra: str = "",
    track_context: bool = False,
    raise_on_error: bool = False,
) -> "Generator[str, None, None]":
    """
    Stream from the Ollama Cloud API (api.ollama.com) using the OpenAI-compat interface.

    Requires OLLAMA_CLOUD_API_KEY to be set. If missing, raises RuntimeError so
    _execute_plan_stream falls through to local.

    Priority: ollama_local → ollama_cloud → paid providers. Use this when the
    local model isn't capable enough and Ollama Cloud free tier has budget left.
    """
    from config import OLLAMA_CLOUD_BASE_URL, OLLAMA_CLOUD_API_KEY, OLLAMA_CLOUD_MODEL
    from openai import OpenAI, APIError, AuthenticationError

    api_key = OLLAMA_CLOUD_API_KEY.strip()
    if not api_key:
        raise RuntimeError("OLLAMA_CLOUD_API_KEY is not set — skipping Ollama Cloud")

    resolved_model = model or OLLAMA_CLOUD_MODEL

    system_parts = [SYSTEM_PROMPT]
    ctx_text = mem.get_context(user_input) if track_context else ""
    if ctx_text:
        system_parts.append(ctx_text)
    if system_extra:
        system_parts.append(system_extra)
    system = "\n\n".join(p for p in system_parts if p)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_input},
    ]

    try:
        client = OpenAI(base_url=OLLAMA_CLOUD_BASE_URL, api_key=api_key)
        stream = client.chat.completions.create(
            model=resolved_model,
            messages=messages,
            stream=True,
            timeout=60,
        )

        full_text = []
        prompt_tokens = 0
        completion_tokens = 0

        for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                full_text.append(delta.content)
                yield delta.content
            # Capture usage if present in the final chunk
            if hasattr(chunk, "usage") and chunk.usage:
                prompt_tokens = getattr(chunk.usage, "prompt_tokens", 0) or 0
                completion_tokens = getattr(chunk.usage, "completion_tokens", 0) or 0

        response_text = "".join(full_text)
        if not prompt_tokens:
            prompt_tokens = max(1, len(system + user_input) // 4)
        if not completion_tokens:
            completion_tokens = max(1, len(response_text) // 4)

        usage_tracker.record(
            provider="ollama_cloud",
            model=resolved_model,
            local=False,
            source="brain_ollama_cloud",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )

        # Also log to budget.jsonl
        try:
            from harness import budget as _budget
            _budget.record(
                provider="ollama_cloud",
                model=resolved_model,
                tokens_in=prompt_tokens,
                tokens_out=completion_tokens,
            )
        except Exception:
            logging.debug("[BrainOllama] silent failure in unknown", exc_info=True)

    except (AuthenticationError, Exception) as exc:
        log.warning("[OllamaCloud] Error: %s", exc)
        if raise_on_error:
            raise
        yield f"[Ollama Cloud unavailable: {exc}]"
