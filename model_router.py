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

import logging
import re
import time as _time
import threading as _threading
from contextlib import contextmanager

from config import GPT_MINI
from config import (
    GPT_FULL,
    GEMINI_FLASH,
    GEMINI_PRO,
    LOCAL_DEFAULT,
    LOCAL_GLM_FLASH,
    LOCAL_CODER,
    LOCAL_REASONING,
    LOCAL_TUNED,
    LOCAL_PREFER_TUNED,
    LOCAL_CODER_RECOMMENDED,
    DEFAULT_MODE,
    tts_runtime_config,
    stt_runtime_config,
    LOCAL_QWEN3_FAST,
    LOCAL_QWEN3_MID,
    LOCAL_QWEN3_STRONG,
    LOCAL_PHI4_MINI,
    LOCAL_DEVSTRAL,
    LOCAL_GEMMA4_STRONG,
    LOCAL_GEMMA4_MOE,
    LOCAL_QWEN3_6,
    LOCAL_FAST_CHAT_CONTEXT_TOKENS,
    LOCAL_FAST_CHAT_MAX_TOKENS,
    HAIKU,
    SONNET,
    OPUS,
)
from brains.brain import ask_stream
from brains.brain_gemini import ask_gemini_stream
from brains.brain_claude import ask_claude_stream
from brains.brain_ollama import ask_local_stream, list_local_models
from local_runtime import local_model_eval
from local_runtime import local_stt, local_tts
import cost_policy
import skills
import vault
import graph_context as _gctx
import semantic_memory as _smem
import memory as _mem
import context_budget as _context_budget
import context_assembler as _ctx_asm
import provider_router
import telemetry
import jarvis_core_brain as _core_brain
import mem0_layer as _m0
import repeat_context as _repeat_context
from local_model_identity import find_exact_ollama_model

_forced_model = ""
_forced_provider = ""
_forced_label = ""

_current_mode = DEFAULT_MODE

# Thread-local override: when set, smart_stream skips local models and uses
# GPT_MINI directly. Used by mobile_web requests so route_stream() does full
# tool dispatch but the conversational LLM is fast cloud, not slow Ollama.
_mobile_tl = _threading.local()


@contextmanager
def mobile_web_override(system_extra: str = ""):
    """Thread-safe context: force GPT_MINI for this request's smart_stream calls."""
    previous_active = getattr(_mobile_tl, "active", False)
    previous_system_extra = getattr(_mobile_tl, "system_extra", "")
    _mobile_tl.active = True
    _mobile_tl.system_extra = system_extra or ""
    try:
        yield
    finally:
        _mobile_tl.active = previous_active
        _mobile_tl.system_extra = previous_system_extra


def _is_mobile_web_active() -> bool:
    return getattr(_mobile_tl, "active", False)


def _mobile_web_system_extra() -> str:
    return getattr(_mobile_tl, "system_extra", "")

_RUNTIME_VOICE_TERMS = (
    "voice",
    "tts",
    "stt",
    "speech",
    "audio",
    "microphone",
    "mic",
    "wake word",
    "wake-word",
)

_ENGINEERING_COMPANION_TERMS = (
    "debug",
    "debugging",
    "design",
    "architecture",
    "architect",
    "tradeoff",
    "trade-off",
    "root cause",
    "system",
    "systems",
    "backend",
    "infra",
    "infrastructure",
    "api",
    "queue",
    "worker",
    "job queue",
    "reliability",
    "observability",
    "performance",
    "distributed",
    "flaky",
    "incident",
    "throughput",
    "latency",
    "bottleneck",
    "sql",
    "python service",
    "service",
    "production",
    "problem solving",
    "problem-solving",
)

_DEBUGGING_TERMS = (
    "debug",
    "debugging",
    "flaky",
    "root cause",
    "regression",
    "reproduce",
    "reproducible",
    "crash",
    "error",
    "failing",
    "failure",
    "timeout",
    "not working",
    "incident",
    "bug",
)

_SYSTEM_DESIGN_TERMS = (
    "design",
    "architecture",
    "architect",
    "tradeoff",
    "trade-off",
    "queue",
    "throughput",
    "latency",
    "scalability",
    "distributed",
    "microservice",
    "cache",
    "consistency",
    "api",
    "schema",
    "worker",
)

_THREAT_MODELING_TERMS = (
    "security",
    "threat",
    "attack",
    "abuse",
    "misuse",
    "adversarial",
    "exploit",
    "vulnerability",
    "xss",
    "csrf",
    "sql injection",
    "prompt injection",
    "jailbreak",
    "auth",
    "authentication",
    "authorization",
    "permission",
    "trust boundary",
    "secret",
    "token",
    "credential",
)

_AI_RUNTIME_TERMS = (
    "agent",
    "routing",
    "grounding",
    "retrieval",
    "tool calling",
    "tool routing",
    "runtime",
    "context window",
    "fallback",
    "model selection",
    "orchestration",
    "memory injection",
    "semantic memory",
    "local-first ai",
)


def get_mode() -> str:
    return _current_mode


def forced_model_status() -> dict:
    if not _forced_model:
        return {"active": False, "model": "", "provider": "", "label": ""}
    return {
        "active": True,
        "model": _forced_model,
        "provider": _forced_provider,
        "label": _forced_label or _forced_model,
    }


def clear_forced_model() -> dict:
    global _forced_model, _forced_provider, _forced_label
    _forced_model = ""
    _forced_provider = ""
    _forced_label = ""
    return forced_model_status()


def _resolve_forced_model(name: str) -> dict:
    candidate = (name or "").strip()
    if not candidate:
        return {"ok": False, "error": "Model name is empty."}

    cloud_map = {
        GPT_MINI: ("openai", "GPT-mini"),
        GPT_FULL: ("openai", "GPT-4o"),
        GEMINI_FLASH: ("gemini", "Gemini Flash"),
        GEMINI_PRO: ("gemini", "Gemini Pro"),
        HAIKU: ("anthropic", "Claude Haiku"),
        SONNET: ("anthropic", "Claude Sonnet"),
        OPUS: ("anthropic", "Claude Opus"),
    }
    if candidate in cloud_map:
        provider, label = cloud_map[candidate]
        return {"ok": True, "model": candidate, "provider": provider, "label": label, "local": False}

    available = _cached_local_models()
    if _has_model(candidate, available):
        return {"ok": True, "model": candidate, "provider": "ollama", "label": candidate, "local": True}

    return {"ok": False, "error": f"Model not found: {candidate}."}


def set_forced_model(name: str) -> dict:
    global _forced_model, _forced_provider, _forced_label
    resolved = _resolve_forced_model(name)
    if not resolved.get("ok"):
        return resolved
    _forced_model = resolved["model"]
    _forced_provider = resolved["provider"]
    _forced_label = resolved.get("label", resolved["model"])
    return forced_model_status()


def is_open_source_mode() -> bool:
    return _current_mode in {"open-source", "open_source", "opensource"}


def set_mode(mode: str) -> str:
    global _current_mode
    mode = mode.strip().lower().replace("_", "-")
    if mode == "opensource":
        mode = "open-source"
    if mode not in ("cloud", "local", "auto", "open-source"):
        return "Unknown mode. Use cloud, local, auto, or open-source."
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
    "how are you", "what's up", "what time", "what day", "help me",
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



# ── Local model list cache with TTL ───────────────────────────────────────────
# list_local_models() costs ~264ms (Ollama API roundtrip).
# Cache for 30 seconds — stale by at most one pull cycle, saves every query.
_LOCAL_LIST_LOCK = _threading.Lock()
_LOCAL_LIST_TTL = 30.0
_local_models_cache: list[str] = []
_local_models_cached_at: float = 0.0
_local_available_cache: bool | None = None


def _cached_local_models() -> list[str]:
    global _local_models_cache, _local_models_cached_at
    now = _time.monotonic()
    if _local_models_cache and (now - _local_models_cached_at) < _LOCAL_LIST_TTL:
        return _local_models_cache
    with _LOCAL_LIST_LOCK:
        now = _time.monotonic()
        if _local_models_cache and (now - _local_models_cached_at) < _LOCAL_LIST_TTL:
            return _local_models_cache
        try:
            _local_models_cache = list_local_models()
        except Exception:
            _local_models_cache = []
        _local_models_cached_at = _time.monotonic()
    return _local_models_cache


def _has_local() -> bool:
    """Check if any local models are available. Backed by TTL-cached list."""
    return len(_cached_local_models()) > 0


def refresh_local_cache() -> None:
    """Call this after pulling a new model so the cache updates immediately."""
    global _local_available_cache, _local_models_cache, _local_models_cached_at
    _local_available_cache = None
    _local_models_cache = []
    _local_models_cached_at = 0.0


def _has_model(name: str, available: list[str]) -> bool:
    """Return whether the exact configured Ollama model is installed."""
    return find_exact_ollama_model(name, available) is not None


def _use_fast_local_context(*, model: str, tool: str | None, local: bool) -> bool:
    """Keep routine default-model chat responsive without weakening specialist lanes."""
    lane = (tool or "chat").strip().lower()
    return bool(
        local
        and lane in {"chat", "extraction"}
        and _has_model(LOCAL_DEFAULT, [model])
    )


def _best_local(text: str) -> str:
    """Pick the best available local model for the task.

    Priority order (highest → lowest):
      1. Eval-promoted model (if not a coding task)
      2. LOCAL_TUNED (jarvis-local) if PREFER_TUNED set
      3. Coding tasks → configured coder > specialist fallbacks
      4. Explicit deep reasoning → configured reasoner > strong fallbacks
      5. General tasks → resident default > heavyweight fallback
      6. Fast/simple → Phi4-mini > Qwen3-fast > gemma4
      7. Fallback: first available model
    """
    available = _cached_local_models()
    lower = text.lower()
    promoted = local_model_eval.promoted_model()

    _CODE_TERMS = ("code", "debug", "function", "script", "refactor", "build", "fix",
                   "implement", "class", "test", "pytest", "unittest", "diff", "patch")
    _DEEP_REASONING_TRIGGERS = (
        "step by step", "walk me through", "detailed analysis",
        "compare and contrast", "system design", "architecture decision",
        "evaluate tradeoffs", "research", "deep dive", "root cause",
        "investigate", "comprehensive", "in depth",
    )

    is_code_task = any(t in lower for t in _CODE_TERMS)
    # Prompt length alone is not a complexity signal. Internal extraction and
    # formatting prompts are long but routine; escalating them evicts the
    # resident chat model and creates a cold-load penalty on the next turn.
    is_deep = any(t in lower for t in _DEEP_REASONING_TRIGGERS)

    # 1. Promoted model (from eval loop) — skip for coding
    if promoted and _has_model(promoted, available) and not is_code_task:
        return promoted

    # 2. Tuned model preference
    if LOCAL_PREFER_TUNED and LOCAL_TUNED and _has_model(LOCAL_TUNED, available) and not is_code_task:
        return LOCAL_TUNED

    # 3. Coding tasks — specialist models first; GLM_FLASH as fallback.
    if is_code_task:
        for coder in (
            LOCAL_CODER,
            LOCAL_DEVSTRAL,        # legacy specialist fallback
            LOCAL_CODER_RECOMMENDED,
            LOCAL_GLM_FLASH,       # general fallback
            LOCAL_QWEN3_MID,
        ):
            if coder and _has_model(coder, available):
                return coder

    # 4. Deep reasoning — qwen3-strong > GLM_FLASH (larger context + MoE efficiency).
    if is_deep:
        for deep in (LOCAL_REASONING, LOCAL_QWEN3_STRONG, LOCAL_GLM_FLASH, LOCAL_QWEN3_MID):
            if deep and _has_model(deep, available):
                return deep

    # 5. General tasks — keep the measured small default resident; GLM is the
    # heavyweight fallback for requests that do not need a specialist lane.
    for general in (LOCAL_DEFAULT, LOCAL_QWEN3_MID, LOCAL_GLM_FLASH):
        if general and _has_model(general, available):
            return general

    # 6. Fast/simple — use the lightweight local fallback if present.
    for fast in (LOCAL_QWEN3_MID, LOCAL_PHI4_MINI, LOCAL_QWEN3_FAST, LOCAL_DEFAULT):
        if fast and _has_model(fast, available):
            return fast

    # 7. Absolute fallback
    fallback = [LOCAL_TUNED, LOCAL_GLM_FLASH, LOCAL_DEFAULT, LOCAL_CODER, LOCAL_REASONING]
    for m in fallback:
        if m and _has_model(m, available):
            return m
    return available[0] if available else LOCAL_DEFAULT


def _apple_foundation_available_for(user_input: str, tool: str | None, tier: str, system_extra: str = "") -> bool:
    """Gate Apple's 4096-token local model to short, simple chat requests."""
    if tier != "mini":
        return False
    if tool and tool != "chat":
        return False
    combined = f"{system_extra}\n{user_input}" if system_extra else user_input
    if len(combined) > 2000:
        return False
    try:
        from brains import brain_apple_foundation
        return brain_apple_foundation.is_available()
    except Exception:
        return False


def describe_runtime_for(user_input: str = "", skill_id: str | None = None) -> str:
    """Return a truthful summary of Jarvis's current routing state."""
    mode = _current_mode
    local_models = list_local_models()
    forced = forced_model_status()
    _, resolved_skills = skills.build_system_extra(user_input, skill_id=skill_id, tool="chat")
    if resolved_skills:
        active_names = ", ".join(skill.id for skill in resolved_skills[:2])
        if len(resolved_skills) > 2:
            active_names += ", plus supporting skills"
        active_skill = f" with {active_names} active"
    else:
        active_skill = ""

    tier = _classify_complexity(user_input or "general conversation", active_skills=resolved_skills)
    explicit_cloud = mode == "cloud"
    if mode == "auto":
        apple_foundation_available = _apple_foundation_available_for(
            user_input or "general conversation",
            "chat",
            tier,
        )
        policy = cost_policy.route_decision(
            user_input or "general conversation",
            tier,
            tool="chat",
            local_available=bool(local_models) or apple_foundation_available,
        )
        tier = policy.get("tier", tier)
        explicit_cloud = policy.get("provider") == "cloud"

    local_model = _best_local(user_input or "general conversation") if local_models else ""
    apple_foundation_available = _apple_foundation_available_for(
        user_input or "general conversation",
        "chat",
        tier,
    )
    plan = provider_router.build_plan(
        mode=mode,
        tier=tier,
        local_available=bool(local_models),
        local_model=local_model,
        apple_foundation_available=apple_foundation_available,
        explicit_cloud=explicit_cloud,
    )
    if not plan.candidates:
        return "I'm in open-source mode, but no local Ollama model is currently available."

    chain = " -> ".join(f"{candidate.label} ({candidate.model})" for candidate in plan.candidates[:4])
    forced_note = ""
    if forced.get("active"):
        forced_note = f" Forced model override: {forced['label']} ({forced['model']})."
    return (
        f"I'm in {mode} mode{active_skill}. "
        f"For this request the active route chain is {chain}. "
        f"Policy: {plan.reason}.{forced_note}"
    )


def _is_runtime_voice_query(user_input: str) -> bool:
    lower = (user_input or "").strip().lower()
    if not lower:
        return False
    if not any(term in lower for term in _RUNTIME_VOICE_TERMS):
        return False
    direct_patterns = (
        r"\bwhat voice are you using\b",
        r"\bwhich voice are you using\b",
        r"\bwhat tts\b",
        r"\bwhich tts\b",
        r"\bwhat stt\b",
        r"\bwhich stt\b",
        r"\bwhat audio\b",
        r"\bwhich audio\b",
        r"\bwhat microphone\b",
        r"\bwhich microphone\b",
        r"\bwhat mic\b",
        r"\bwhich mic\b",
        r"\bwhat wake word\b",
        r"\bwhich wake word\b",
    )
    if any(re.search(pattern, lower) for pattern in direct_patterns):
        return True
    return any(marker in lower for marker in ("jarvis", "your", "you", "current", "configured", "using", "backend"))


def _runtime_voice_grounding() -> str:
    tts_cfg = tts_runtime_config()
    stt_cfg = stt_runtime_config()
    say_status = local_tts.status()
    stt_status = local_stt.status()
    tts_backends = ", ".join(tts_cfg.get("backends", [])) or "unknown"
    stt_backends = ", ".join(stt_cfg.get("backends", [])) or "unknown"
    local_cfg = tts_cfg.get("local", {})
    kokoro_cfg = tts_cfg.get("kokoro", {})
    return (
        "Jarvis runtime voice facts:\n"
        "- Answer Jarvis voice, audio, TTS, STT, microphone, and wake-word questions using only the current runtime facts below.\n"
        "- Do not rely on vault summaries, stale README text, or generic industry suggestions for these questions.\n"
        "- Do not recommend external managed TTS or STT services unless the user explicitly asks for cloud or paid alternatives.\n"
        f"- Current routing mode: {_current_mode}.\n"
        f"- Configured TTS backends in priority order: {tts_backends}.\n"
        f"- Primary configured TTS backend: {tts_cfg.get('primary_backend', 'unknown')}.\n"
        f"- Local macOS say voice: {local_cfg.get('voice', 'unknown')} at {local_cfg.get('rate_wpm', 'unknown')} words per minute.\n"
        f"- Local macOS say ready state: {'ready' if say_status.get('ready') else 'not ready'}.\n"
        f"- Kokoro configured: {'enabled' if kokoro_cfg.get('enabled') else 'disabled'}, voice {kokoro_cfg.get('voice', 'unknown')}.\n"
        f"- Configured STT backends in priority order: {stt_backends}.\n"
        f"- Active STT engine: {stt_status.get('active_engine', 'unknown')}.\n"
        f"- Faster-whisper model: {stt_cfg.get('faster_whisper', {}).get('model', 'unknown')} on {stt_cfg.get('faster_whisper', {}).get('device', 'unknown')} with compute type {stt_cfg.get('faster_whisper', {}).get('compute_type', 'unknown')}.\n"
        f"- STT language setting: {stt_cfg.get('language') or stt_status.get('language') or 'auto'}.\n"
        "- If a fact is not in this runtime block, say you would need to verify it rather than guessing."
    )


def _trim_context_line(text: str, limit: int = 180) -> str:
    compact = " ".join((text or "").split()).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _user_snapshot_grounding() -> str:
    try:
        memory_status = _mem.memory_status()
    except Exception:
        return ""

    durable = memory_status.get("long_term_profile") or {}
    working = memory_status.get("working_memory") or {}
    lines: list[str] = []

    summary = _trim_context_line(durable.get("summary", ""), 220)
    if summary:
        lines.append(f"- Durable profile: {summary}")

    active_projects = working.get("active_projects") or []
    if active_projects:
        lines.append(
            "- Active projects: " + "; ".join(_trim_context_line(item, 90) for item in active_projects[:2])
        )

    assist_preferences = working.get("assist_preferences") or []
    if assist_preferences:
        lines.append(
            "- Assist preferences: " + "; ".join(_trim_context_line(item, 90) for item in assist_preferences[:2])
        )

    recurring_topics = working.get("recurring_topics") or []
    if recurring_topics:
        lines.append("- Recurring topics: " + ", ".join(recurring_topics[:4]))

    if not lines:
        return ""
    return "Compact user snapshot:\n" + "\n".join(lines)


def _is_engineering_companion_query(user_input: str, tool: str | None) -> bool:
    if tool != "chat":
        return False
    lower = (user_input or "").lower()
    if not lower or _is_runtime_voice_query(lower):
        return False
    return any(term in lower for term in _ENGINEERING_COMPANION_TERMS)


def _engineering_playbook_category(user_input: str) -> str | None:
    lower = (user_input or "").lower()
    if any(term in lower for term in _DEBUGGING_TERMS):
        return "debugging"
    if any(term in lower for term in _SYSTEM_DESIGN_TERMS):
        return "systems_design"
    if any(term in lower for term in _THREAT_MODELING_TERMS):
        return "threat_modeling"
    if any(term in lower for term in _AI_RUNTIME_TERMS):
        return "ai_runtime_agent"
    return None


def _engineering_grounding_queries(user_input: str) -> list[str]:
    queries = [
        "senior cybersecurity ai engineering companion",
        "universal engineer thinker problem solver",
    ]
    category = _engineering_playbook_category(user_input)
    if category == "debugging":
        queries.append("debugging root cause playbook")
    elif category == "systems_design":
        queries.append("systems design tradeoff heuristics")
    elif category == "threat_modeling":
        queries.append("threat modeling security thinking")
    elif category == "ai_runtime_agent":
        queries.append("ai runtime agent engineering principles")
    return queries


def _engineering_companion_grounding(user_input: str) -> str:
    hits: list[dict] = []
    seen_paths: set[str] = set()
    try:
        for query in _engineering_grounding_queries(user_input):
            for hit in vault.search(query, topn=1):
                path = hit.get("path") or ""
                if path and path in seen_paths:
                    continue
                if path:
                    seen_paths.add(path)
                hits.append(hit)
                if len(hits) >= 4:
                    break
            if len(hits) >= 4:
                break
    except Exception:
        hits = []

    lines = [
        "Engineering companion guidance:",
        "- Act like a senior technical partner, not a generic assistant.",
        "- Diagnose the failing layer first and prefer the smallest correct next step.",
        "- Use cross-layer reasoning across systems, product, AI, security, and operations when the problem spans them.",
        "- Prefer verification and concrete evidence over speculation.",
    ]
    for hit in hits[:4]:
        excerpt = _trim_context_line(hit.get("excerpt", ""), 220)
        title = hit.get("title") or hit.get("matched_heading") or "Brain note"
        if excerpt:
            lines.append(f"- {title}: {excerpt}")
    return "\n".join(lines)


def _is_coding_request(user_input: str, tool: str | None) -> bool:
    if tool != "chat":
        return False
    lower = (user_input or "").lower()
    if not lower:
        return False
    coding_terms = (
        "code", "repo", "patch", "diff", "edit", "refactor", "implement",
        "test", "pytest", "unittest", "build", "compile", "fix", "bug",
        "multi-file", "multiple files", "run tests", "typecheck",
    )
    return any(term in lower for term in coding_terms)


def _coding_companion_grounding() -> str:
    return (
        "Coding companion guidance: approach this like a senior coding agent. "
        "Inspect the repo before editing, make minimal diffs, and prefer multi-file edits only when required. "
        "If you change code, run the smallest relevant tests or verification and report what ran and what changed. "
        "If a command cannot be run, say what you would run and why."
    )


def _semantic_memory_hint(hits: list[dict] | None) -> str:
    if not hits:
        return ""
    lines = [
        "Semantic memory guidance:",
        "- If the retrieved memory is directly relevant, prefer it over generic advice.",
        "- Use retrieved user and project context to personalize the answer when it genuinely helps.",
    ]
    top = _trim_context_line(hits[0].get("content", ""), 220)
    if top:
        lines.append(f"- Most relevant retrieved memory: {top}")
    return "\n".join(lines)


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


def _cloud_token_budget_exhausted() -> bool:
    """Tokens-per-hour rate guard. Inactive unless JARVIS_CLOUD_TOKENS_PER_HOUR
    is set to a positive integer; once the last hour's cloud token usage meets
    the budget, auto-mode routing degrades to local instead of bursting into
    provider rate limits. Fails open: any error means "not exhausted"."""
    import logging
    import os
    raw = os.getenv("JARVIS_CLOUD_TOKENS_PER_HOUR", "").strip()
    try:
        budget = int(raw)
    except ValueError:
        return False
    if budget <= 0:
        return False
    try:
        import usage_tracker
        used = sum(
            int(r.get("total_tokens") or 0)
            for r in usage_tracker.entries(hours=1)
            if not r.get("local")
        )
    except Exception:
        return False
    if used >= budget:
        logging.getLogger(__name__).warning(
            "cloud token budget exhausted (%d/%d tokens in last hour) — routing local",
            used, budget,
        )
        return True
    return False


def _breaker_recovery_wait_seconds(plan, circuit_breaker_mod, cap: float = 60.0) -> float | None:
    """Seconds until the soonest OPEN circuit breaker on this plan's cloud
    providers recovers (transitions to HALF_OPEN), or None if no breaker will
    recover within `cap` seconds. Used by the streaming path to decide whether
    a wait-and-retry is worth it after all candidates fail."""
    if circuit_breaker_mod is None:
        return None
    soonest: float | None = None
    for candidate in plan.candidates:
        if candidate.local:
            continue
        try:
            state = circuit_breaker_mod.get_state(candidate.provider)
        except Exception:
            continue
        if state.get("state") != circuit_breaker_mod.OPEN or not state.get("opened_at"):
            continue
        remaining = float(state["opened_at"]) + circuit_breaker_mod.OPEN_SECONDS - _time.time()
        if remaining <= 0:
            remaining = 0.0
        if remaining <= cap and (soonest is None or remaining < soonest):
            soonest = remaining
    return soonest


def _capture_cloud_stream(prompt, tier, candidate, raw_stream, source: str = "model_router_cloud_teacher"):
    """Thin shim around brains._teacher_capture.wrap_stream so callers in this
    module can keep their existing import surface."""
    from brains import _teacher_capture
    yield from _teacher_capture.wrap_stream(prompt, tier, candidate, raw_stream, source=source)


def smart_stream(
    user_input: str,
    skill_id: str | None = None,
    tool: str | None = "chat",
    extra_system: str = "",
    prefer_local: bool = False,
    local_only: bool = False,
    skip_dynamic_context: bool = False,
) -> tuple:
    """
    Core routing function. Returns (stream, model_label).
    Strategy: local → mini → haiku → sonnet → opus
    Only escalates when the task genuinely requires it.

    prefer_local: caller asserts this is internal runtime work (e.g. agent
    task execution) whose prompt scaffolding would otherwise trip the
    chat-tuned complexity heuristics. Routes local-first when a local model
    is available; cloud fallback chain stays intact.

    local_only: enforce an Ollama-only route. Forced cloud models, mobile cloud
    fast paths, and provider fallbacks are ignored. If Ollama is unavailable,
    return the open-source-unavailable response without transmitting the prompt.

    skip_dynamic_context: skip vault/graph/semantic-memory/mem0 retrieval.
    For tool-loop continuations the task context hasn't changed since turn 1,
    so re-retrieval only adds tokens, an embedding call per turn, and prompt-
    prefix churn that defeats Ollama's KV prefix cache.
    """
    _smart_stream_t0 = _time.monotonic()
    # ── Mobile web fast-path: skip slow local models, go straight to GPT-mini ──
    # IMPORTANT: must NOT use yield/yield-from here — that would make smart_stream
    # a generator function and break all callers that expect a (stream, label) tuple.
    # Instead return a (stream, label) tuple just like every other path does.
    if _is_mobile_web_active() and not local_only:
        mobile_extra = _mobile_web_system_extra()
        merged_extra = extra_system
        if mobile_extra:
            merged_extra = merged_extra + ("\n\n" if merged_extra else "") + mobile_extra
        _mobile_system = (
            merged_extra + "\n\n" if merged_extra else ""
        ) + "You are Jarvis on the user's MacBook, accessed via mobile. Be concise."
        return ask_stream(
            user_input,
            GPT_MINI,
            system_extra=_mobile_system,
            track_context=False,
            bypass_local=True,
        ), GPT_MINI

    runtime_voice_query = tool == "chat" and _is_runtime_voice_query(user_input)
    mode = _current_mode
    local_available = _has_local()
    local_model = _best_local(user_input) if local_available else ""
    forced = forced_model_status()
    context_model = ""
    context_is_local = False
    if forced.get("active") and forced.get("provider") == "ollama":
        context_model = forced.get("model") or ""
        context_is_local = True
    elif (local_only or mode != "cloud") and local_available:
        context_model = local_model
        context_is_local = True
    fast_local_context = _use_fast_local_context(
        model=context_model,
        tool=tool,
        local=context_is_local,
    )

    # The fast lane uses the compact user snapshot below. Specialist lanes keep
    # the expanded operating profile for deeper project and reasoning context.
    _brain_ctx = "" if fast_local_context else _core_brain.core_context()
    grounding_extra = (
        "Grounding rules:\n"
        "- Treat the current user message as primary truth.\n"
        "- Treat tool output and runtime facts as stronger than memory or inference.\n"
        "- Treat vault and semantic memory as supporting context that may be stale.\n"
        "- Do not claim you performed actions, scans, checks, or integrations unless the current context explicitly shows the result.\n"
        "- Do not invent system specs, network details, permissions, account access, device state, or completed work.\n"
        "- If evidence is missing, say what you can verify next instead of presenting guesses as facts."
    )
    if _brain_ctx:
        grounding_extra = _brain_ctx + "\n\n" + grounding_extra
    user_snapshot = ""
    if runtime_voice_query:
        system_extra, resolved_skills = "", []
    else:
        system_extra, resolved_skills = skills.build_system_extra(user_input, skill_id=skill_id, tool=tool)
    system_extra = grounding_extra + ("\n\n" + system_extra if system_extra else "")
    if runtime_voice_query:
        voice_grounding = _runtime_voice_grounding()
        system_extra = voice_grounding + ("\n\n" + system_extra if system_extra else "")
    else:
        user_snapshot = _user_snapshot_grounding()
        if user_snapshot:
            system_extra = user_snapshot + ("\n\n" + system_extra if system_extra else "")
        if _is_engineering_companion_query(user_input, tool):
            engineering_grounding = _engineering_companion_grounding(user_input)
            if engineering_grounding:
                system_extra = engineering_grounding + ("\n\n" + system_extra if system_extra else "")
        if _is_coding_request(user_input, tool):
            coding_grounding = _coding_companion_grounding()
            if coding_grounding:
                system_extra = coding_grounding + ("\n\n" + system_extra if system_extra else "")
    if extra_system:
        system_extra = extra_system + ("\n\n" + system_extra if system_extra else "")

    # ── Parallel context assembly ──────────────────────────────────────────────
    # vault, graph, and semantic memory are all read-only and independent.
    # Running them concurrently cuts wall time from sum → max of the three.
    from concurrent.futures import ThreadPoolExecutor

    def _get_repeat():
        if runtime_voice_query:
            return ""
        return _repeat_context.context_for_prompt(user_input, max_chars=1400)

    def _get_vault():
        if runtime_voice_query:
            return ""
        return vault.build_context(user_input, tool=tool)

    def _get_graph():
        return _gctx.context_for_query(user_input, tool=tool)

    def _get_smem():
        if runtime_voice_query:
            return [], ""
        # min_score=0.3 filters low-relevance noise before injection
        hits = _smem.retrieve(user_input, top_k=5, min_score=0.3)
        return hits, _smem.format_for_prompt(hits, max_chars=1200)

    def _get_mem0():
        """mem0 cross-session episodic memory — runs concurrently with vault/smem."""
        if runtime_voice_query:
            return ""
        hits = _m0.search(user_input, top_k=5)
        return _m0.format_for_prompt(hits, max_chars=600)

    def _get_working_mem():
        """Working memory: facts, preferences, projects from memory.json."""
        if runtime_voice_query:
            return ""
        ctx = _mem.get_context()
        if not ctx:
            return ""
        # Cap at ~500 tokens (~2000 chars); split on newline to avoid mid-line truncation
        if len(ctx) > 2000:
            ctx = ctx[:2000].rsplit("\n", 1)[0]
        return f"<memory>\n{ctx}\n</memory>"

    repeat_extra = vault_extra = graph_extra = smem_ctx = mem0_extra = working_mem_extra = ""
    smem_hits: list[dict] = []
    if fast_local_context:
        # The compact user snapshot already carries identity, preferences, and
        # active projects. Fall back to working memory only when it is absent.
        if not user_snapshot:
            try:
                working_mem_extra = _get_working_mem() or ""
            except Exception as _exc:
                logging.debug("[Context] working_memory retrieval failed: %s", _exc)
    elif skip_dynamic_context:
        try:
            working_mem_extra = _get_working_mem() or ""
        except Exception as _exc:
            logging.debug("[Context] working_memory retrieval failed: %s", _exc)
    else:
        # Deliberately NOT a `with` block: ThreadPoolExecutor.__exit__ joins
        # the worker threads, so one getter hung on a network call without a
        # socket timeout would block this request forever despite the result()
        # timeouts below (live incident 2026-06-10: agent task stuck in
        # "streaming" for 50+ min behind a hung embedding request).
        _pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="ctx")
        try:
            _fr = _pool.submit(_get_repeat)
            _fv = _pool.submit(_get_vault)
            _fg = _pool.submit(_get_graph)
            _fs = _pool.submit(_get_smem)
            _fm = _pool.submit(_get_mem0)
            _fw = _pool.submit(_get_working_mem)
            try:
                repeat_extra = _fr.result(timeout=2.0) or ""
            except Exception as _exc:
                logging.debug("[Context] repeat_context retrieval failed: %s", _exc)
            try:
                vault_extra = _fv.result(timeout=2.0) or ""
            except Exception as _exc:
                logging.debug("[Context] vault retrieval failed: %s", _exc)
            try:
                graph_extra = _fg.result(timeout=2.0) or ""
            except Exception as _exc:
                logging.debug("[Context] graph_context retrieval failed: %s", _exc)
            try:
                smem_hits, smem_ctx = _fs.result(timeout=4.0)
            except Exception as _exc:
                logging.debug("[Context] semantic_memory retrieval failed: %s", _exc)
            try:
                mem0_extra = _fm.result(timeout=4.0) or ""
            except Exception as _exc:
                logging.debug("[Context] mem0 retrieval failed: %s", _exc)
            try:
                working_mem_extra = _fw.result(timeout=2.0) or ""
            except Exception as _exc:
                logging.debug("[Context] working_memory retrieval failed: %s", _exc)
        finally:
            _pool.shutdown(wait=False, cancel_futures=True)

    semantic_hint = _semantic_memory_hint(smem_hits)
    compiled_context = _context_budget.compile_context_blocks(
        _ctx_asm.rank_context_blocks(
            working_mem=working_mem_extra,
            repeat_ctx=repeat_extra,
            vault_ctx=vault_extra,
            graph_ctx=graph_extra,
            semantic_hint=semantic_hint,
            smem_hits=smem_hits,
            mem0_ctx=mem0_extra,
        ),
        base_text=system_extra,
        user_input=user_input,
        target_tokens=_context_budget.target_tokens_for(
            tool,
            model=context_model,
            local=context_is_local,
        ),
    )
    if compiled_context["text"]:
        system_extra = system_extra + ("\n\n" if system_extra else "") + compiled_context["text"]

    # Context pressure gate: if context is >75% full, drop episodic blocks and recompile;
    # if >90% full and local available, force routing to a larger-context local model.
    try:
        from harness import budget as _budget_mod
        ctx_pressure = _budget_mod.context_pressure(
            compiled_context.get("context_used_tokens", 0),
            compiled_context.get("context_budget_tokens", 1),
        )
        if ctx_pressure in ("compress", "switch"):
            _ctx_used = compiled_context.get("context_used_tokens", 0)
            _ctx_budget = compiled_context.get("context_budget_tokens", 1) or 1
            _pressure_float = _ctx_used / _ctx_budget
            logging.info(
                "[ContextPressure] %.0f%% full — recompiling with pressure-aware ranking",
                _pressure_float * 100,
            )
            compressed = _context_budget.compile_context_blocks(
                _ctx_asm.rank_context_blocks(
                    working_mem=working_mem_extra,
                    repeat_ctx=repeat_extra,
                    vault_ctx=vault_extra,
                    graph_ctx=graph_extra,
                    semantic_hint=semantic_hint,
                    smem_hits=smem_hits,
                    mem0_ctx=mem0_extra,
                    pressure=_pressure_float,
                ),
                base_text=system_extra,
                user_input=user_input,
                target_tokens=_context_budget.target_tokens_for(tool, model=context_model, local=context_is_local),
            )
            compiled_context = compressed
            system_extra = (system_extra.split("\n\n")[0] if "\n\n" in system_extra else system_extra)
            if compressed["text"]:
                system_extra = system_extra + ("\n\n" if system_extra else "") + compressed["text"]
            if ctx_pressure == "switch" and local_available and local_model:
                logging.warning("[ContextPressure] >90%% full — forcing local model with larger context window")
                prefer_local = True
                local_only = True
    except Exception as _ctx_exc:
        logging.debug("[ContextPressure] check failed: %s", _ctx_exc)

    def _resilient_stream(primary_factory, fallback_factories):
        def _stream():
            last_error = None
            try:
                yield from primary_factory()
                return
            except Exception as exc:
                last_error = exc
                logging.warning("[ModelRouter] Primary model stream failed: %s", exc)

            for name, factory in fallback_factories:
                try:
                    logging.info("[ModelRouter] Falling back to %s.", name)
                    yield from factory()
                    return
                except Exception as exc:
                    last_error = exc
                    logging.warning("[ModelRouter] Fallback %s failed: %s", name, exc)

            yield f"I hit an upstream model error while answering this, and the fallback path also failed: {last_error}"

        return _stream()

    if forced.get("active") and (not local_only or forced.get("provider") == "ollama"):
        candidate = provider_router.RouteCandidate(
            provider=forced["provider"],
            model=forced["model"],
            local=forced["provider"] == "ollama",
            label=f"Forced {forced.get('label') or forced['model']}",
        )
        plan = provider_router.RoutePlan(
            mode=_current_mode,
            tier="local" if candidate.local else "mini",
            candidates=(candidate,),
            reason="Forced model override.",
        )
        return _execute_forced_stream(
            plan,
            user_input,
            system_extra,
            tool=tool,
            context_budget_report=compiled_context,
            fast_local_context=fast_local_context,
        ), candidate.label

    if local_only:
        tier = "local"
        apple_foundation_available = False
        explicit_cloud = False
    elif prefer_local and local_available and local_model and mode != "cloud":
        tier = "local"
        apple_foundation_available = False
        explicit_cloud = False
    else:
        tier = _classify_complexity(user_input, active_skills=resolved_skills)
        apple_foundation_available = _apple_foundation_available_for(user_input, tool, tier, system_extra)
        explicit_cloud = mode == "cloud"
        if mode == "auto":
            policy = cost_policy.route_decision(
                user_input,
                tier,
                tool=tool,
                local_available=local_available or apple_foundation_available,
            )
            tier = policy["tier"]
            explicit_cloud = policy.get("provider") == "cloud"
            apple_foundation_available = _apple_foundation_available_for(user_input, tool, tier, system_extra)
            if explicit_cloud and local_available and local_model and _cloud_token_budget_exhausted():
                tier = "local"
                explicit_cloud = False
                apple_foundation_available = False

    plan = provider_router.build_plan(

        mode="open-source" if local_only else mode,
        tier=tier,
        local_available=local_available,
        local_model=local_model,
        apple_foundation_available=apple_foundation_available,
        explicit_cloud=explicit_cloud,
    )

    if not plan.candidates:
        return _open_source_unavailable_stream(), "Open-Source"

    primary_label = plan.candidates[0].label

    def _candidate_stream(candidate):
        if candidate.provider == "ollama":
            fast_chat = fast_local_context and _use_fast_local_context(
                model=candidate.model,
                tool=tool,
                local=True,
            )
            resident_default = _has_model(LOCAL_DEFAULT, [candidate.model])
            if resident_default:
                try:
                    from brains.brain_ollama import start_keepalive
                    start_keepalive(
                        candidate.model,
                        max_context=LOCAL_FAST_CHAT_CONTEXT_TOKENS,
                    )
                except Exception:
                    logging.debug(
                        "[ModelRouter] local keepalive registration failed",
                        exc_info=True,
                    )
            return ask_local_stream(
                user_input,
                candidate.model,
                system_extra=system_extra,
                track_context=True,
                raise_on_error=True,
                context_budget_report=compiled_context,
                include_memory=False,
                max_context=(
                    LOCAL_FAST_CHAT_CONTEXT_TOKENS
                    if resident_default
                    else None
                ),
                max_output=LOCAL_FAST_CHAT_MAX_TOKENS if fast_chat else None,
                think=False if fast_chat else None,
                keep_alive="5m" if resident_default else "0",
            )
        if candidate.provider == "apple_foundation":
            from brains.brain_apple_foundation import ask_apple_foundation_stream
            return ask_apple_foundation_stream(
                user_input,
                candidate.model,
                system_extra=system_extra,
                track_context=True,
                raise_on_error=True,
            )
        # Gate all non-local cloud providers through the budget check.
        # Soft limit → warning already logged inside budget.check().
        # Hard limit → raise so _execute_plan_stream falls through to next candidate (local).
        if not candidate.local:
            # Global hourly cap across all cloud providers (JARVIS_CLOUD_TOKENS_PER_HOUR).
            # Checked per candidate so it fires in every mode — not just the
            # auto-mode plan gate above, which only covers explicit-cloud routes.
            if _cloud_token_budget_exhausted():
                raise RuntimeError(
                    f"[Budget] cloud hourly token budget exhausted "
                    f"(JARVIS_CLOUD_TOKENS_PER_HOUR) — skipping {candidate.provider}, "
                    f"falling through to local"
                )
            try:
                from harness import budget as _budget
                bcheck = _budget.check(candidate.provider)
                if bcheck["hard"]:
                    raise RuntimeError(
                        f"[Budget] {candidate.provider} hard rate limit exceeded "
                        f"({bcheck.get('used_1h') or bcheck.get('used_session', 0):,} tokens) "
                        f"— falling through to local"
                    )
            except ImportError:
                pass
        if candidate.provider == "ollama_cloud":
            from brains.brain_ollama import ask_ollama_cloud_stream
            return ask_ollama_cloud_stream(
                user_input,
                candidate.model,
                system_extra=system_extra,
                track_context=True,
                raise_on_error=True,
            )
        if candidate.provider == "openai":
            # bypass_local=True: provider_router already considered local at
            # the planner level. If we're here we explicitly chose OpenAI;
            # brain.ask_stream should not re-run the local-first gate.
            return ask_stream(
                user_input,
                candidate.model,
                system_extra=system_extra,
                track_context=True,
                bypass_local=True,
            )
        if candidate.provider == "gemini":
            return ask_gemini_stream(user_input, candidate.model, system_extra=system_extra, track_context=True)
        if candidate.provider == "anthropic":
            return ask_claude_stream(user_input, candidate.model, system_extra=system_extra, track_context=True)
        raise RuntimeError(f"Unknown provider candidate: {candidate.provider}")

    def _execute_plan_stream():
        try:
            from harness import circuit_breaker as _circuit_breaker
        except Exception:
            _circuit_breaker = None
        last_error = None
        selected = None
        # Two passes at most: if every candidate fails on the first pass and a
        # circuit breaker will recover within 60s, announce the wait, sleep,
        # and retry the full plan once before giving up.
        for _attempt in (0, 1):
            for candidate in plan.candidates:
                if _circuit_breaker is not None:
                    try:
                        if not _circuit_breaker.is_available(candidate.provider):
                            logging.warning(
                                "[ModelRouter] Skipping %s: circuit breaker OPEN", candidate.label
                            )
                            continue
                    except Exception:
                        logging.debug("[ModelRouter] circuit breaker check failed", exc_info=True)
                try:
                    selected = {
                        "provider": candidate.provider,
                        "model": candidate.model,
                        "local": candidate.local,
                        "label": candidate.label,
                    }
                    telemetry.log_route_decision(
                        user_input=user_input,
                        mode=plan.mode,
                        tier=plan.tier,
                        plan={"candidates": [c.__dict__ for c in plan.candidates]},
                        selected=selected,
                        reason=plan.reason,
                    )
                    # Wrap cloud streams so successful answers feed the local
                    # teacher pack (no-op unless JARVIS_TEACHER_CAPTURE=1 and
                    # tier in {strong, deep}).
                    raw_stream = _candidate_stream(candidate)
                    if candidate.local:
                        yield from raw_stream
                    else:
                        yield from _capture_cloud_stream(
                            prompt=user_input,
                            tier=plan.tier,
                            candidate=candidate,
                            raw_stream=raw_stream,
                        )
                    if _circuit_breaker is not None:
                        try:
                            _circuit_breaker.record_success(candidate.provider)
                        except Exception:
                            logging.debug("[ModelRouter] circuit breaker record_success failed", exc_info=True)
                    return
                except Exception as exc:
                    last_error = exc
                    logging.warning("[ModelRouter] Candidate %s failed: %s", candidate.label, exc)
                    if _circuit_breaker is not None:
                        try:
                            if _circuit_breaker.is_rate_limit_error(exc):
                                _circuit_breaker.record_failure(candidate.provider)
                        except Exception:
                            logging.debug("[ModelRouter] circuit breaker record_failure failed", exc_info=True)
            if _attempt == 0:
                _wait = _breaker_recovery_wait_seconds(plan, _circuit_breaker)
                if _wait is not None:
                    _wait_display = max(1, int(_wait) + 1)
                    logging.warning(
                        "[ModelRouter] All candidates failed — breaker recovery in %ds, retrying plan once",
                        _wait_display,
                    )
                    yield f"[Jarvis: all providers busy, retrying in {_wait_display}s...]"
                    _time.sleep(_wait_display)
                    continue
            break
        yield f"I hit an upstream model error while answering this, and the fallback path also failed: {last_error}"

    try:
        from harness.audit import audit_log as _audit_log
        _audit_log(
            "model_call",
            model_used=primary_label,
            latency_ms=round((_time.monotonic() - _smart_stream_t0) * 1000),
            tool=tool,
            tier=plan.tier,
            mode=plan.mode,
        )
    except Exception:
        logging.debug("[ModelRouter] silent failure in unknown", exc_info=True)
    return _execute_plan_stream(), primary_label


def _execute_forced_stream(
    plan: provider_router.RoutePlan,
    user_input: str,
    system_extra: str,
    *,
    tool: str | None = "chat",
    context_budget_report: dict | None = None,
    fast_local_context: bool = False,
):
    def _candidate_stream(candidate):
        if candidate.provider == "ollama":
            fast_chat = fast_local_context and _use_fast_local_context(
                model=candidate.model,
                tool=tool,
                local=True,
            )
            resident_default = _has_model(LOCAL_DEFAULT, [candidate.model])
            if resident_default:
                try:
                    from brains.brain_ollama import start_keepalive
                    start_keepalive(
                        candidate.model,
                        max_context=LOCAL_FAST_CHAT_CONTEXT_TOKENS,
                    )
                except Exception:
                    logging.debug(
                        "[ModelRouter] local keepalive registration failed",
                        exc_info=True,
                    )
            return ask_local_stream(
                user_input,
                candidate.model,
                system_extra=system_extra,
                track_context=True,
                raise_on_error=True,
                context_budget_report=context_budget_report,
                include_memory=False,
                max_context=(
                    LOCAL_FAST_CHAT_CONTEXT_TOKENS
                    if resident_default
                    else None
                ),
                max_output=LOCAL_FAST_CHAT_MAX_TOKENS if fast_chat else None,
                think=False if fast_chat else None,
                keep_alive="5m" if resident_default else "0",
            )
        if candidate.provider == "openai":
            return ask_stream(
                user_input,
                candidate.model,
                system_extra=system_extra,
                track_context=True,
                bypass_local=True,
            )
        if candidate.provider == "gemini":
            return ask_gemini_stream(user_input, candidate.model, system_extra=system_extra, track_context=True)
        if candidate.provider == "anthropic":
            return ask_claude_stream(user_input, candidate.model, system_extra=system_extra, track_context=True)
        raise RuntimeError(f"Unknown provider candidate: {candidate.provider}")

    def _stream():
        last_error = None
        for candidate in plan.candidates:
            try:
                telemetry.log_route_decision(
                    user_input=user_input,
                    mode=plan.mode,
                    tier=plan.tier,
                    plan={"candidates": [c.__dict__ for c in plan.candidates]},
                    selected={
                        "provider": candidate.provider,
                        "model": candidate.model,
                        "local": candidate.local,
                        "label": candidate.label,
                    },
                    reason=plan.reason,
                )
                yield from _candidate_stream(candidate)
                return
            except Exception as exc:
                last_error = exc
        yield f"I hit an upstream model error while answering this, and the forced model failed: {last_error}"

    return _stream()


def format_with_mini(
    prompt: str,
    skill_id: str | None = None,
    tool: str | None = None,
    extra_system: str = "",
    ground_query: str = "",
):
    """Format tool output with free-first routing for lightweight generation."""
    import memory as _mem
    context = _mem.get_context()
    system_extra, _ = skills.build_system_extra(prompt, skill_id=skill_id, tool=tool)
    technical_summary = bool(ground_query and _is_engineering_companion_query(ground_query, "chat"))
    if technical_summary:
        engineering_extra = _engineering_companion_grounding(ground_query)
        if engineering_extra:
            system_extra = engineering_extra + ("\n\n" + system_extra if system_extra else "")
    if extra_system:
        system_extra = extra_system + ("\n\n" + system_extra if system_extra else "")
    if technical_summary:
        prompt = (
            "Format this for Aman like a senior engineering companion. "
            "Lead with the conclusion, recommendation, or most important finding first. "
            "Then name the key tradeoff, root cause, or next verification step in one short follow-up sentence when relevant.\n\n"
            f"{prompt}"
        )
    if context:
        prompt = prompt + f"\n\nUser context for personalization:{context}"
    local_available = _has_local()
    local_model = _best_local(prompt) if local_available else ""
    apple_foundation_available = _apple_foundation_available_for(prompt, tool, "mini", system_extra)
    plan = provider_router.build_plan(
        mode=_current_mode,
        tier="mini",
        local_available=local_available,
        local_model=local_model,
        apple_foundation_available=apple_foundation_available,
        explicit_cloud=_current_mode == "cloud",
    )
    if not plan.candidates:
        return _open_source_unavailable_stream()

    def _stream():
        last_error = None
        for candidate in plan.candidates:
            try:
                # Same global hourly cloud cap as smart_stream's candidate gate:
                # raise inside the try so the loop falls through to the next
                # (local) candidate instead of spending cloud tokens.
                if not candidate.local and _cloud_token_budget_exhausted():
                    raise RuntimeError(
                        f"[Budget] cloud hourly token budget exhausted "
                        f"(JARVIS_CLOUD_TOKENS_PER_HOUR) — skipping {candidate.provider}"
                    )
                if candidate.provider == "ollama":
                    yield from ask_local_stream(
                        prompt,
                        candidate.model,
                        system_extra=system_extra,
                        track_context=False,
                        raise_on_error=True,
                    )
                    return
                if candidate.provider == "apple_foundation":
                    from brains.brain_apple_foundation import ask_apple_foundation_stream
                    yield from ask_apple_foundation_stream(
                        prompt,
                        candidate.model,
                        system_extra=system_extra,
                        track_context=False,
                        raise_on_error=True,
                    )
                    return
                if candidate.provider == "gemini":
                    yield from ask_gemini_stream(prompt, candidate.model, system_extra=system_extra, track_context=False)
                    return
                if candidate.provider == "anthropic":
                    yield from ask_claude_stream(prompt, candidate.model, system_extra=system_extra, track_context=False)
                    return
                # bypass_local=True: planner already handled local routing.
                yield from ask_stream(
                    prompt,
                    candidate.model,
                    system_extra=system_extra,
                    track_context=False,
                    bypass_local=True,
                )
                return
            except Exception as exc:
                last_error = exc
                continue
        yield f"I hit an upstream formatting error and fallbacks also failed: {last_error}"

    return _stream()
