"""
Smart model router — local-first, cost-efficient strategy:

  1. Local (Ollama) — free, unrestricted, private. Used whenever capable.
  2. GPT-mini        — cheapest cloud formatting and fallback path.
  3. Gemini Flash    — preferred fast cloud path for everyday cloud reasoning.
  4. GPT-4o          — preferred strong cloud path for analysis and planning.
  5. Gemini Pro      — deep cloud reasoning before Anthropic fallback.
  6. Claude tiers    — fallback path when OpenAI/Gemini are unavailable.

Jarvis picks the cheapest model that can handle the task reliably.

Mode commands:
  "switch to local mode"        → force all AI through Ollama
  "switch to cloud mode"        → force all AI through Claude/GPT
  "switch to auto mode"         → smart routing (default)
  "switch to open-source mode"  → force Jarvis onto local/open tooling only
  "what mode are you in"        → status
"""

from config import GPT_MINI, GPT_FULL, GEMINI_FLASH, GEMINI_PRO, HAIKU, SONNET, OPUS
from config import LOCAL_DEFAULT, LOCAL_CODER, LOCAL_REASONING, LOCAL_TUNED, DEFAULT_MODE
from brain import ask_stream
from brain_gemini import ask_gemini_stream
from brain_claude import ask_claude_stream
from brain_ollama import ask_local_stream, list_local_models
import local_model_eval
import cost_policy
import skills
import vault
import semantic_memory as _smem

_current_mode = DEFAULT_MODE


def get_mode() -> str:
    return _current_mode


def is_open_source_mode() -> bool:
    return _current_mode in {"open-source", "open_source", "opensource"}


def set_mode(mode: str) -> str:
    global _current_mode
    mode = mode.strip().lower().replace("_", "-")
    if mode == "opensource":
        mode = "open-source"
    if mode not in ("cloud", "local", "auto", "open-source"):
        return f"Unknown mode. Use cloud, local, auto, or open-source."
    _current_mode = mode
    return {
        "cloud": "Cloud mode. Using OpenAI and Gemini first, with Claude as fallback.",
        "local": "Local mode. Using on-device models — fully private and unrestricted.",
        "auto":  "Auto mode. I'll use local models when I can and cloud only when I need to.",
        "open-source": "Open-source mode. Jarvis will stay on local models and local runtime logic, avoiding closed-model dependencies.",
    }[mode]


def _open_source_unavailable_stream():
    yield "Open-source mode is enabled, but no local Ollama model is currently available. Start Ollama and pull a local model first."


# ── Task complexity classifier ────────────────────────────────────────────────

# Tasks that REQUIRE cloud — too complex for small local models
NEEDS_CLOUD_HARD = {
    # Deep coding/architecture
    "refactor", "architecture", "design pattern",
    "race condition", "concurrency", "system design", "optimize this",
    # Deep reasoning
    "step by step", "walk me through", "explain in detail",
    "best approach to", "trade off", "tradeoff",
    # Long-form writing
    "write a full", "write an entire", "write a detailed",
    "comprehensive", "in depth",
}

# Tasks that benefit from cloud mid-tier (Sonnet/Haiku)
NEEDS_CLOUD_MID = {
    "summarize", "summarise", "analyze", "analyse", "compare",
    "review", "proofread", "plan", "strategy", "research",
    "pros and cons", "difference between", "recommend",
    "should i", "what's better",
    # Technical debugging / troubleshooting
    "debug", "troubleshoot", "crashes", "crash", "error",
    "503", "502", "500", "timeout", "timed out", "not working",
    "how do i fix", "how to fix", "what causes", "why does",
    "most likely", "top 3", "top 5", "best way to",
    "memory leak", "distributed system", "race condition",
    "optimistic locking", "pessimistic locking", "nginx", "fastapi",
    "dockerized", "dockerised", "queue over", "rpc call",
    "narrow them down", "debugging plan",
    # Science and advanced technology
    "transformer", "kv cache", "attention mechanism", "scaling law",
    "thermodynamics", "information theory", "entropy",
    "crispr", "genome editing", "off-target effects",
    "euv lithography", "stochastic defects", "semiconductor",
    "materials science", "molecular biology", "quantum",
}

# Tasks that a local model handles perfectly
LOCAL_CAPABLE = {
    # Conversation
    "how are you", "what's up", "tell me", "who is", "what is",
    "what time", "what day", "help me", "can you",
    # Simple coding
    "write a function", "write a script", "fix this", "debug",
    "what does this code", "explain this code",
    # Quick tasks
    "remind me", "note this", "remember", "open", "search",
    "timer", "volume", "screenshot", "weather",
}

# Explicit local preference
EXPLICIT_LOCAL = {
    "no filter", "uncensored", "unfiltered", "privately",
    "off the record", "don't hold back", "be brutally honest",
    "local model", "use local", "on device", "without restriction",
}


_local_available_cache: bool | None = None

def _has_local() -> bool:
    """Check if any local models are available. Cached after first check."""
    global _local_available_cache
    if _local_available_cache is None:
        try:
            _local_available_cache = len(list_local_models()) > 0
        except Exception:
            _local_available_cache = False
    return _local_available_cache

def refresh_local_cache() -> None:
    """Call this after pulling a new model so the cache updates."""
    global _local_available_cache
    _local_available_cache = None


def _best_local(text: str) -> str:
    """Pick the best available local model for the task."""
    available = list_local_models()
    lower = text.lower()
    promoted = local_model_eval.promoted_model()

    if promoted and any(promoted in m for m in available):
        if not any(t in lower for t in ("code", "debug", "function", "script", "refactor", "build", "fix")):
            return promoted

    if LOCAL_TUNED and any(LOCAL_TUNED in m for m in available):
        if not any(t in lower for t in ("code", "debug", "function", "script", "refactor", "build", "fix")):
            return LOCAL_TUNED

    if any(t in lower for t in ("code", "debug", "function", "script", "refactor", "build", "fix")):
        if any(LOCAL_CODER in m for m in available):
            return LOCAL_CODER

    if any(t in lower for t in ("analyze", "plan", "explain", "why", "how", "reason")):
        if any(LOCAL_REASONING in m for m in available):
            return LOCAL_REASONING

    # Return first available
    for preferred in (promoted, LOCAL_TUNED, LOCAL_DEFAULT, LOCAL_CODER, LOCAL_REASONING):
        if not preferred:
            continue
        if any(preferred.split(":")[0] in m for m in available):
            return preferred
    return available[0]


def describe_runtime_for(user_input: str = "", skill_id: str | None = None) -> str:
    """Return a truthful summary of Jarvis's current routing state."""
    mode = _current_mode
    local_models = list_local_models()
    _, resolved_skills = skills.build_system_extra(user_input, skill_id=skill_id, tool="chat")
    if resolved_skills:
        active_names = ", ".join(skill.id for skill in resolved_skills[:2])
        if len(resolved_skills) > 2:
            active_names += ", plus supporting skills"
        active_skill = f" with {active_names} active"
    else:
        active_skill = ""

    if mode == "local":
        if local_models:
            chosen = _best_local(user_input or "general conversation")
            return f"I'm in local mode and I'd answer this{active_skill} with Ollama using {chosen}."
        return "I'm in local mode, but no Ollama model is currently available."

    if is_open_source_mode():
        if local_models:
            chosen = _best_local(user_input or "general conversation")
            return f"I'm in open-source mode and this request is staying{active_skill} on the local Jarvis runtime with Ollama using {chosen}."
        return "I'm in open-source mode, but no local Ollama model is currently available."

    if mode == "cloud":
        tier = _classify_complexity(user_input or "general conversation", active_skills=resolved_skills)
        if tier in ("local", "mini"):
            return f"I'm in cloud mode and this request would use {GPT_MINI}{active_skill}."
        if tier == "haiku":
            return f"I'm in cloud mode and this request would use {GEMINI_FLASH}{active_skill}, with OpenAI and Claude as fallbacks."
        if tier == "sonnet":
            return f"I'm in cloud mode and this request would use {GPT_FULL}{active_skill}, with Gemini and Claude as fallbacks."
        return f"I'm in cloud mode and this request would use {GEMINI_PRO}{active_skill}, with GPT-4o and Claude as fallbacks."

    tier = _classify_complexity(user_input or "general conversation", active_skills=resolved_skills)
    policy = cost_policy.route_decision(
        user_input or "general conversation",
        tier,
        tool="chat",
        local_available=bool(local_models),
    )
    policy_tier = policy.get("tier", tier)
    provider = policy.get("provider", "base")
    if provider == "local" and local_models:
        chosen = _best_local(user_input or "general conversation")
        return f"I'm in auto mode and this request is currently routing{active_skill} to local inference with Ollama using {chosen}."
    if provider == "cloud":
        if policy_tier in ("local", "mini"):
            return f"I'm in auto mode and this request would use {GPT_MINI}{active_skill}."
        if policy_tier == "haiku":
            return f"I'm in auto mode and this request would use {GEMINI_FLASH}{active_skill}, with OpenAI and Claude as fallbacks."
        if policy_tier == "sonnet":
            return f"I'm in auto mode and this request would use {GPT_FULL}{active_skill}, with Gemini and Claude as fallbacks."
        return f"I'm in auto mode and this request would use {GEMINI_PRO}{active_skill}, with GPT-4o and Claude as fallbacks."
    if tier == "mini":
        return f"I'm in auto mode and this request would use {GPT_MINI}{active_skill}."
    if tier == "haiku":
        return f"I'm in auto mode and this request would use {GEMINI_FLASH}{active_skill}, with OpenAI and Claude as fallbacks."
    if tier == "sonnet":
        return f"I'm in auto mode and this request would use {GPT_FULL}{active_skill}, with Gemini and Claude as fallbacks."
    if tier == "opus":
        return f"I'm in auto mode and this request would use {GEMINI_PRO}{active_skill}, with GPT-4o and Claude as fallbacks."
    if local_models:
        chosen = _best_local(user_input or "general conversation")
        return f"I'm in auto mode and this request is currently routing{active_skill} to local inference with Ollama using {chosen}."
    return f"I'm in auto mode, but no local Ollama model is currently available, so this request would fall back to {GPT_MINI}{active_skill}."


def _classify_complexity(text: str, skill_id: str | None = None, active_skills: list | None = None) -> str:
    """
    Returns: 'local', 'mini', 'haiku', 'sonnet', 'opus'
    Based on task complexity — cheapest viable option.
    """
    lower = text.lower()
    word_count = len(lower.split())
    cost_hint = skills.skill_cost_hint(active_skills or skill_id)

    if cost_hint == "opus":
        return "opus"
    if cost_hint == "sonnet":
        return "sonnet"
    if cost_hint == "haiku":
        return "haiku"
    if cost_hint == "mini":
        return "mini"
    if cost_hint == "local":
        hinted_local = True
    else:
        hinted_local = False

    technical_markers = (
        "python service", "memory leak", "distributed system", "race condition",
        "optimistic locking", "pessimistic locking", "dockerized", "dockerised",
        "fastapi", "nginx", "queue", "rpc", "debugging plan", "narrow them down",
        "concrete debugging plan", "software engineer", "technical question",
        "transformer", "kv cache", "attention", "context window",
        "thermodynamics", "information theory", "entropy",
        "crispr", "genome editing", "cas9", "off-target",
        "lithography", "euv", "stochastic defect", "semiconductor",
        "physics", "biology", "chemistry", "materials science",
    )

    # Explicitly asking for local
    if any(t in lower for t in EXPLICIT_LOCAL):
        return "local"

    # Genuinely hard — only Opus can do it well
    if any(t in lower for t in NEEDS_CLOUD_HARD) and word_count > 10:
        return "opus"

    # Mid complexity — Sonnet is the right call
    if any(t in lower for t in NEEDS_CLOUD_MID):
        return "sonnet"

    if any(t in lower for t in technical_markers):
        return "sonnet" if not hinted_local else "haiku"

    # Short simple factual — GPT-mini (cheap, fast)
    if word_count <= 8 and not any(t in lower for t in NEEDS_CLOUD_MID):
        return "mini"

    # Long, complex questions (15+ words with a question mark) need at least Haiku
    if word_count >= 15 and "?" in lower:
        return "haiku"

    # Everything else — try local first, fall back to haiku if unavailable
    return "local"


def smart_stream(
    user_input: str,
    skill_id: str | None = None,
    tool: str | None = "chat",
    extra_system: str = "",
) -> tuple:
    """
    Core routing function. Returns (stream, model_label).
    Strategy: local → mini → haiku → sonnet → opus
    Only escalates when the task genuinely requires it.
    """
    mode = _current_mode
    system_extra, resolved_skills = skills.build_system_extra(user_input, skill_id=skill_id, tool=tool)
    if extra_system:
        system_extra = extra_system + ("\n\n" + system_extra if system_extra else "")
    vault_extra = vault.build_context(user_input, tool=tool)
    if vault_extra:
        system_extra = system_extra + ("\n\n" if system_extra else "") + vault_extra

    # Semantic KB: inject relevant facts from memory/ (TF-IDF over structured JSON)
    smem_ctx = _smem.context_for_query(user_input, top_k=3, max_chars=1200)
    if smem_ctx:
        system_extra = system_extra + ("\n\n" if system_extra else "") + smem_ctx

    def _resilient_stream(primary_factory, fallback_factories):
        def _stream():
            last_error = None
            try:
                yield from primary_factory()
                return
            except Exception as exc:
                last_error = exc
                print(f"[ModelRouter] Primary model stream failed: {exc}")

            for name, factory in fallback_factories:
                try:
                    print(f"[ModelRouter] Falling back to {name}.")
                    yield from factory()
                    return
                except Exception as exc:
                    last_error = exc
                    print(f"[ModelRouter] Fallback {name} failed: {exc}")

            yield f"I hit an upstream model error while answering this, and the fallback path also failed: {last_error}"

        return _stream()

    # ── Force local ───────────────────────────────────────────────────────────
    if mode == "local":
        if _has_local():
            model = _best_local(user_input)
            return ask_local_stream(user_input, model, system_extra=system_extra, track_context=True), "Local"
        else:
            return _resilient_stream(
                lambda: ask_stream(user_input, GPT_MINI, system_extra=system_extra, track_context=True),
                [
                    ("Gemini Flash", lambda: ask_gemini_stream(user_input, GEMINI_FLASH, system_extra=system_extra, track_context=True)),
                    ("Claude Haiku", lambda: ask_claude_stream(user_input, HAIKU, system_extra=system_extra, track_context=True)),
                ],
            ), "GPT-mini"

    # ── Open-source mode: local-only model path ─────────────────────────────
    if is_open_source_mode():
        if _has_local():
            model = _best_local(user_input)
            return ask_local_stream(user_input, model, system_extra=system_extra, track_context=True), "Open-Source"
        return _open_source_unavailable_stream(), "Open-Source"

    # ── Force cloud ───────────────────────────────────────────────────────────
    if mode == "cloud":
        tier = _classify_complexity(user_input, active_skills=resolved_skills)
        if tier in ("local", "mini"):
            return ask_stream(user_input, GPT_MINI, system_extra=system_extra, track_context=True), "GPT-mini"
        elif tier == "haiku":
            fallbacks = [
                ("GPT-mini", lambda: ask_stream(user_input, GPT_MINI, system_extra=system_extra, track_context=True)),
                ("Claude Haiku", lambda: ask_claude_stream(user_input, HAIKU, system_extra=system_extra, track_context=True)),
            ]
            if _has_local():
                fallbacks.append(("Local", lambda: ask_local_stream(user_input, _best_local(user_input), system_extra=system_extra, track_context=True)))
            return _resilient_stream(
                lambda: ask_gemini_stream(user_input, GEMINI_FLASH, system_extra=system_extra, track_context=True),
                fallbacks,
            ), "Gemini Flash"
        elif tier == "sonnet":
            fallbacks = [
                ("Gemini Pro", lambda: ask_gemini_stream(user_input, GEMINI_PRO, system_extra=system_extra, track_context=True)),
                ("Claude Sonnet", lambda: ask_claude_stream(user_input, SONNET, system_extra=system_extra, track_context=True)),
            ]
            if _has_local():
                fallbacks.append(("Local", lambda: ask_local_stream(user_input, _best_local(user_input), system_extra=system_extra, track_context=True)))
            return _resilient_stream(
                lambda: ask_stream(user_input, GPT_FULL, system_extra=system_extra, track_context=True),
                fallbacks,
            ), "GPT-4o"
        else:
            fallbacks = [
                ("GPT-4o", lambda: ask_stream(user_input, GPT_FULL, system_extra=system_extra, track_context=True)),
                ("Claude Opus", lambda: ask_claude_stream(user_input, OPUS, system_extra=system_extra, track_context=True)),
            ]
            if _has_local():
                fallbacks.append(("Local", lambda: ask_local_stream(user_input, _best_local(user_input), system_extra=system_extra, track_context=True)))
            return _resilient_stream(
                lambda: ask_gemini_stream(user_input, GEMINI_PRO, system_extra=system_extra, track_context=True),
                fallbacks,
            ), "Gemini Pro"

    # ── Auto mode: local-first, cloud only when needed ────────────────────────
    tier = _classify_complexity(user_input, active_skills=resolved_skills)
    policy = cost_policy.route_decision(
        user_input,
        tier,
        tool=tool,
        local_available=_has_local(),
    )
    tier = policy["tier"]
    provider = policy.get("provider", "base")

    if provider == "local":
        model = _best_local(user_input)
        return ask_local_stream(user_input, model, system_extra=system_extra, track_context=True), "Local"

    if provider == "cloud":
        if tier in ("local", "mini"):
            return ask_stream(user_input, GPT_MINI, system_extra=system_extra, track_context=True), "GPT-mini"
        if tier == "haiku":
            fallbacks = [
                ("GPT-mini", lambda: ask_stream(user_input, GPT_MINI, system_extra=system_extra, track_context=True)),
                ("Claude Haiku", lambda: ask_claude_stream(user_input, HAIKU, system_extra=system_extra, track_context=True)),
            ]
            if _has_local():
                fallbacks.append(("Local", lambda: ask_local_stream(user_input, _best_local(user_input), system_extra=system_extra, track_context=True)))
            return _resilient_stream(
                lambda: ask_gemini_stream(user_input, GEMINI_FLASH, system_extra=system_extra, track_context=True),
                fallbacks,
            ), "Gemini Flash"
        if tier == "sonnet":
            fallbacks = [
                ("Gemini Pro", lambda: ask_gemini_stream(user_input, GEMINI_PRO, system_extra=system_extra, track_context=True)),
                ("Claude Sonnet", lambda: ask_claude_stream(user_input, SONNET, system_extra=system_extra, track_context=True)),
            ]
            if _has_local():
                fallbacks.append(("Local", lambda: ask_local_stream(user_input, _best_local(user_input), system_extra=system_extra, track_context=True)))
            return _resilient_stream(
                lambda: ask_stream(user_input, GPT_FULL, system_extra=system_extra, track_context=True),
                fallbacks,
            ), "GPT-4o"
        fallbacks = [
            ("GPT-4o", lambda: ask_stream(user_input, GPT_FULL, system_extra=system_extra, track_context=True)),
            ("Claude Opus", lambda: ask_claude_stream(user_input, OPUS, system_extra=system_extra, track_context=True)),
        ]
        if _has_local():
            fallbacks.append(("Local", lambda: ask_local_stream(user_input, _best_local(user_input), system_extra=system_extra, track_context=True)))
        return _resilient_stream(
            lambda: ask_gemini_stream(user_input, GEMINI_PRO, system_extra=system_extra, track_context=True),
            fallbacks,
        ), "Gemini Pro"

    if tier == "local":
        if _has_local():
            model = _best_local(user_input)
            return ask_local_stream(user_input, model, system_extra=system_extra, track_context=True), "Local"
        else:
            # No local model — use cheapest cloud
            return ask_stream(user_input, GPT_MINI, system_extra=system_extra, track_context=True), "GPT-mini"

    elif tier == "mini":
        return ask_stream(user_input, GPT_MINI, system_extra=system_extra, track_context=True), "GPT-mini"

    elif tier == "haiku":
        # Try local first, fall back to Haiku
        if _has_local():
            model = _best_local(user_input)
            return ask_local_stream(user_input, model, system_extra=system_extra, track_context=True), "Local"
        return _resilient_stream(
            lambda: ask_gemini_stream(user_input, GEMINI_FLASH, system_extra=system_extra, track_context=True),
            [
                ("GPT-mini", lambda: ask_stream(user_input, GPT_MINI, system_extra=system_extra, track_context=True)),
                ("Claude Haiku", lambda: ask_claude_stream(user_input, HAIKU, system_extra=system_extra, track_context=True)),
            ],
        ), "Gemini Flash"

    elif tier == "sonnet":
        fallbacks = [
            ("Gemini Pro", lambda: ask_gemini_stream(user_input, GEMINI_PRO, system_extra=system_extra, track_context=True)),
            ("Claude Sonnet", lambda: ask_claude_stream(user_input, SONNET, system_extra=system_extra, track_context=True)),
        ]
        if _has_local():
            fallbacks.append(("Local", lambda: ask_local_stream(user_input, _best_local(user_input), system_extra=system_extra, track_context=True)))
        return _resilient_stream(
            lambda: ask_stream(user_input, GPT_FULL, system_extra=system_extra, track_context=True),
            fallbacks,
        ), "GPT-4o"

    else:  # opus — only when genuinely needed
        fallbacks = [
            ("GPT-4o", lambda: ask_stream(user_input, GPT_FULL, system_extra=system_extra, track_context=True)),
            ("Claude Opus", lambda: ask_claude_stream(user_input, OPUS, system_extra=system_extra, track_context=True)),
        ]
        if _has_local():
            fallbacks.append(("Local", lambda: ask_local_stream(user_input, _best_local(user_input), system_extra=system_extra, track_context=True)))
        return _resilient_stream(
            lambda: ask_gemini_stream(user_input, GEMINI_PRO, system_extra=system_extra, track_context=True),
            fallbacks,
        ), "Gemini Pro"


def format_with_mini(
    prompt: str,
    skill_id: str | None = None,
    tool: str | None = None,
    extra_system: str = "",
):
    """Format tool output using cheapest cloud model, unless open-source mode is active."""
    import memory as _mem
    context = _mem.get_context()
    system_extra, _ = skills.build_system_extra(prompt, skill_id=skill_id, tool=tool)
    if extra_system:
        system_extra = extra_system + ("\n\n" + system_extra if system_extra else "")
    if context:
        prompt = prompt + f"\n\nUser context for personalization:{context}"
    if is_open_source_mode():
        if _has_local():
            return ask_local_stream(prompt, _best_local(prompt), system_extra=system_extra, track_context=False)
        return _open_source_unavailable_stream()
    return ask_stream(prompt, GPT_MINI, system_extra=system_extra)
