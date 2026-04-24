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

import re

from config import GPT_MINI
from config import (
    LOCAL_DEFAULT,
    LOCAL_CODER,
    LOCAL_REASONING,
    LOCAL_TUNED,
    LOCAL_PREFER_TUNED,
    DEFAULT_MODE,
    tts_runtime_config,
    stt_runtime_config,
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
import provider_router
import telemetry

_current_mode = DEFAULT_MODE

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


import time as _time
import threading as _threading

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


def _best_local(text: str) -> str:
    """Pick the best available local model for the task."""
    available = _cached_local_models()
    lower = text.lower()
    promoted = local_model_eval.promoted_model()

    if promoted and any(promoted in m for m in available):
        if not any(t in lower for t in ("code", "debug", "function", "script", "refactor", "build", "fix")):
            return promoted

    if LOCAL_PREFER_TUNED and LOCAL_TUNED and any(LOCAL_TUNED in m for m in available):
        if not any(t in lower for t in ("code", "debug", "function", "script", "refactor", "build", "fix")):
            return LOCAL_TUNED

    if any(t in lower for t in ("code", "debug", "function", "script", "refactor", "build", "fix")):
        if any(LOCAL_CODER in m for m in available):
            return LOCAL_CODER

    # Reasoning tasks — only use DeepSeek R1 for genuinely complex multi-step problems.
    # Too many triggers here tanks UX: R1:14b takes 3-10 min on Mac vs gemma4's 10-30s.
    # Reserve R1 for: deep analysis, architecture, step-by-step walkthroughs, long queries.
    _DEEP_REASONING_TRIGGERS = (
        "step by step", "walk me through", "detailed analysis",
        "compare and contrast", "system design", "architecture decision",
        "evaluate tradeoffs", "research", "deep dive", "root cause",
        "investigate", "comprehensive", "in depth",
    )
    word_count = len(lower.split())
    uses_deep_trigger = any(t in lower for t in _DEEP_REASONING_TRIGGERS)
    # Only use R1 if the query is explicitly requesting deep reasoning OR is a long complex question (25+ words)
    if (uses_deep_trigger or word_count >= 25) and any(LOCAL_REASONING in m for m in available):
        return LOCAL_REASONING

    # Return first available — prefer fast default model over slow reasoning model
    fallback_preferred = [promoted]
    if LOCAL_PREFER_TUNED:
        fallback_preferred.append(LOCAL_TUNED)
    # Gemma4 first (10-30s), R1 only as last resort
    fallback_preferred.extend([LOCAL_DEFAULT, LOCAL_CODER, LOCAL_REASONING])

    for preferred in fallback_preferred:
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

    tier = _classify_complexity(user_input or "general conversation", active_skills=resolved_skills)
    explicit_cloud = mode == "cloud"
    if mode == "auto":
        policy = cost_policy.route_decision(
            user_input or "general conversation",
            tier,
            tool="chat",
            local_available=bool(local_models),
        )
        tier = policy.get("tier", tier)
        explicit_cloud = policy.get("provider") == "cloud"

    local_model = _best_local(user_input or "general conversation") if local_models else ""
    plan = provider_router.build_plan(
        mode=mode,
        tier=tier,
        local_available=bool(local_models),
        local_model=local_model,
        explicit_cloud=explicit_cloud,
    )
    if not plan.candidates:
        return "I'm in open-source mode, but no local Ollama model is currently available."

    chain = " -> ".join(f"{candidate.label} ({candidate.model})" for candidate in plan.candidates[:4])
    return (
        f"I'm in {mode} mode{active_skill}. "
        f"For this request the active route chain is {chain}. "
        f"Policy: {plan.reason}"
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
) -> tuple:
    """
    Core routing function. Returns (stream, model_label).
    Strategy: local → mini → haiku → sonnet → opus
    Only escalates when the task genuinely requires it.
    """
    grounding_extra = (
        "Grounding rules:\n"
        "- Treat the current user message as primary truth.\n"
        "- Treat tool output and runtime facts as stronger than memory or inference.\n"
        "- Treat vault and semantic memory as supporting context that may be stale.\n"
        "- Do not claim you performed actions, scans, checks, or integrations unless the current context explicitly shows the result.\n"
        "- Do not invent system specs, network details, permissions, account access, device state, or completed work.\n"
        "- If evidence is missing, say what you can verify next instead of presenting guesses as facts."
    )
    runtime_voice_query = tool == "chat" and _is_runtime_voice_query(user_input)
    mode = _current_mode
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
    if extra_system:
        system_extra = extra_system + ("\n\n" + system_extra if system_extra else "")
    # ── Parallel context assembly ──────────────────────────────────────────────
    # vault, graph, and semantic memory are all read-only and independent.
    # Running them concurrently cuts wall time from sum → max of the three.
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    def _get_vault():
        if runtime_voice_query:
            return ""
        return vault.build_context(user_input, tool=tool)

    def _get_graph():
        return _gctx.context_for_query(user_input, tool=tool)

    def _get_smem():
        if runtime_voice_query:
            return [], ""
        hits = _smem.retrieve(user_input, top_k=3)
        return hits, _smem.format_for_prompt(hits, max_chars=1200)

    vault_extra = graph_extra = smem_ctx = ""
    smem_hits: list[dict] = []
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="ctx") as _pool:
        _fv = _pool.submit(_get_vault)
        _fg = _pool.submit(_get_graph)
        _fs = _pool.submit(_get_smem)
        try:
            vault_extra = _fv.result(timeout=2.0) or ""
        except Exception:
            pass
        try:
            graph_extra = _fg.result(timeout=2.0) or ""
        except Exception:
            pass
        try:
            smem_hits, smem_ctx = _fs.result(timeout=4.0)
        except Exception:
            pass

    if vault_extra:
        system_extra = system_extra + ("\n\n" if system_extra else "") + vault_extra
    if graph_extra:
        system_extra = system_extra + ("\n\n" if system_extra else "") + graph_extra
    semantic_hint = _semantic_memory_hint(smem_hits)
    if semantic_hint:
        system_extra = system_extra + ("\n\n" if system_extra else "") + semantic_hint
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

    local_available = _has_local()
    local_model = _best_local(user_input) if local_available else ""

    tier = _classify_complexity(user_input, active_skills=resolved_skills)
    explicit_cloud = mode == "cloud"
    if mode == "auto":
        policy = cost_policy.route_decision(
            user_input,
            tier,
            tool=tool,
            local_available=local_available,
        )
        tier = policy["tier"]
        explicit_cloud = policy.get("provider") == "cloud"

    plan = provider_router.build_plan(

        mode=mode,
        tier=tier,
        local_available=local_available,
        local_model=local_model,
        explicit_cloud=explicit_cloud,
    )

    if not plan.candidates:
        return _open_source_unavailable_stream(), "Open-Source"

    primary_label = plan.candidates[0].label

    def _candidate_stream(candidate):
        if candidate.provider == "ollama":
            # Update keepalive target so the next query finds this model already warm
            try:
                from brains.brain_ollama import start_keepalive
                start_keepalive(candidate.model)
            except Exception:
                pass
            return ask_local_stream(
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
        last_error = None
        selected = None
        for candidate in plan.candidates:
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
                if candidate.provider == "ollama":
                    yield from raw_stream
                else:
                    yield from _capture_cloud_stream(
                        prompt=user_input,
                        tier=plan.tier,
                        candidate=candidate,
                        raw_stream=raw_stream,
                    )
                return
            except Exception as exc:
                last_error = exc
                print(f"[ModelRouter] Candidate {candidate.label} failed: {exc}")
        yield f"I hit an upstream model error while answering this, and the fallback path also failed: {last_error}"

    return _execute_plan_stream(), primary_label


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
    plan = provider_router.build_plan(
        mode=_current_mode,
        tier="mini",
        local_available=local_available,
        local_model=local_model,
        explicit_cloud=_current_mode == "cloud",
    )
    if not plan.candidates:
        return _open_source_unavailable_stream()

    def _stream():
        last_error = None
        for candidate in plan.candidates:
            try:
                if candidate.provider == "ollama":
                    yield from ask_local_stream(
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
