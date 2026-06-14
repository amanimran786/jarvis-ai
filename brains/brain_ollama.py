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
from typing import Any
from config import SYSTEM_PROMPT, LOCAL_DEFAULT, LOCAL_CODER, LOCAL_REASONING, LOCAL_TUNED, LOCAL_PREFER_TUNED
import context_budget
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


def _structured_client():
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
        pass
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
    "glm-4.7-flash": 202752,
    "gemma4:e4b": 8192,
    "gemma3:4b": 8192,
    "llama3.1:8b": 8192,
    "qwen3:8b": 40960,
    "qwen2.5-coder:7b": 32768,
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
        options["num_ctx"] = int(os.getenv("GLM_CTX", os.getenv("OLLAMA_GLM_CONTEXT", "64000")))
    if "deepseek" in lower:
        # Cap DeepSeek R1 to limit reasoning token explosion on Mac.
        options["num_ctx"] = int(os.getenv("DEEPSEEK_CTX", "8192"))
        options["num_predict"] = int(os.getenv("DEEPSEEK_MAX_TOKENS", "1024"))
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
            local=True,
            source="brain_ollama.ask_local_structured",
            prompt_tokens=prompt_eval_count,
            completion_tokens=eval_count,
            total_tokens=((prompt_eval_count or 0) + (eval_count or 0)) if (prompt_eval_count is not None or eval_count is not None) else None,
            messages=messages,
            response_text=content,
            estimated=(prompt_eval_count is None and eval_count is None),
            metadata={"structured": True},
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
):
    """Stream a response from a local Ollama model."""
    # Inject chain-of-thought boost for non-trivial inputs (skip for short commands)
    word_count = len(user_input.split())
    if word_count > 6 and not system_extra:
        system_extra = _REASONING_BOOST
    elif word_count > 6 and _REASONING_BOOST not in system_extra:
        system_extra = _REASONING_BOOST + "\n\n" + system_extra

    pruned_prompt = _prune_prompt(user_input, SYSTEM_PROMPT, system_extra)
    system_base = pruned_prompt + mem.get_context()
    if track_context:
        ctx.begin_turn(user_input)
        system, messages, _ = ctx.build_prompt_state(system_base, system_extra=system_extra)
        messages = [{"role": "system", "content": system}] + messages
        messages, conversation_budget_report = _cap_track_context_messages(messages)
    else:
        system = system_base
        if system_extra:
            system += "\n\n" + system_extra
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user_input}]
        conversation_budget_report = None

    # Escalate to a larger-context local model only after the full prompt is
    # assembled. User input alone hides the real cost of system + memory blocks.
    model = get_best_available(_fits_local(_messages_text(messages), model))

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
        local=True,
        source="brain_ollama.ask_local_stream",
        prompt_tokens=prompt_eval_count,
        completion_tokens=eval_count,
        total_tokens=((prompt_eval_count or 0) + (eval_count or 0)) if (prompt_eval_count is not None or eval_count is not None) else None,
        messages=messages,
        response_text=cleaned_reply,
        estimated=(prompt_eval_count is None and eval_count is None),
        metadata={
            "track_context": track_context,
            **({"context_budget": context_budget_report} if context_budget_report else {}),
            **({"conversation_budget": conversation_budget_report} if conversation_budget_report else {}),
        },
    )

    if track_context:
        ctx.end_turn(cleaned_reply)


def list_local_models() -> list[str]:
    """Return names of all pulled local models."""
    try:
        return [m.model for m in _client().list().models]
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

_JARVIS_ROOT: "Path | None" = None  # type: ignore[name-defined]


def _jarvis_root() -> "Any":
    global _JARVIS_ROOT
    if _JARVIS_ROOT is None:
        from pathlib import Path
        _JARVIS_ROOT = Path(__file__).resolve().parent.parent
    return _JARVIS_ROOT


def _execute_agent_tool(name: str, args: dict) -> str:
    try:
        if name == "web_search":
            from tools import web_search
            query = str(args.get("query", "")).strip()
            return web_search(query, max_results=5, summarise=False) if query else "No query provided."

        elif name == "read_file":
            import os
            raw = str(args.get("path", args.get("filepath", ""))).strip()
            if not raw:
                return "No path provided."
            if os.getenv("JARVIS_WORKSPACE_CONFINED") == "1":
                from tools import read_file
                return read_file(raw)
            else:
                from pathlib import Path
                root = _jarvis_root()
                candidate = (root / raw).resolve()
                if not str(candidate).startswith(str(root)):
                    return "Access denied: path outside project root."
                if not candidate.is_file():
                    return f"File not found: {raw}"
                return candidate.read_text(errors="replace")[:6000]

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


def ask_local_with_tools(
    user_input: str,
    model: str = LOCAL_DEFAULT,
    system_extra: str = "",
    tools: "list[str] | None" = None,  # type: ignore[type-arg]
    max_iterations: int = 6,
):
    """Agentic function-calling loop for local inference.

    Model calls tools (web_search, read_file, get_weather, memory_lookup),
    gets results, then streams the final synthesized answer — same pattern as
    Claude/GPT tool use, fully local via Ollama.

    Falls back to plain ask_local_stream when no callable tools are requested.
    """
    tool_names = [t for t in (tools or []) if t in _AGENT_TOOL_SCHEMAS]
    if not tool_names:
        yield from ask_local_stream(user_input, model=model, system_extra=system_extra)
        return

    tool_schemas = [_AGENT_TOOL_SCHEMAS[t] for t in tool_names]

    system = SYSTEM_PROMPT + mem.get_context()
    if system_extra:
        system += "\n\n" + system_extra

    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_input},
    ]

    model = get_best_available(_fits_local(_messages_text(messages), model))
    _opts = _ollama_options_for_model(model)

    last_msg = None
    for _ in range(max_iterations):
        response = _client().chat(
            model=model,
            messages=messages,
            tools=tool_schemas,
            stream=False,
            options=_opts or None,
        )
        last_msg = response.message
        tool_calls = last_msg.tool_calls or []

        if not tool_calls:
            break

        # Append assistant's tool-call decision
        messages.append({
            "role": "assistant",
            "content": last_msg.content or "",
            "tool_calls": [
                {"function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ],
        })

        # Execute each tool and append results
        for tc in tool_calls:
            result = _execute_agent_tool(tc.function.name, tc.function.arguments)
            messages.append({"role": "tool", "content": result})
    else:
        # Exhausted iterations — prompt for wrap-up
        messages.append({"role": "user", "content": "Summarize your findings based on the results above."})

    # Stream the final synthesis
    stream = _client().chat(
        model=model,
        messages=messages,
        stream=True,
        options=_opts or None,
    )
    raw_buffer = ""
    try:
        for chunk in stream:
            delta = chunk.message.content or ""
            raw_buffer += delta
            if any(raw_buffer.rstrip().endswith(c) for c in ('.', '!', '?')) and len(raw_buffer) > 40:
                cleaned = _strip_markdown(raw_buffer)
                if cleaned:
                    yield cleaned
                raw_buffer = ""
    except Exception as exc:
        yield f"Tool agent error: {exc}"
        return
    if raw_buffer:
        cleaned = _strip_markdown(raw_buffer)
        if cleaned:
            yield cleaned


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
