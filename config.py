import os
import sys
from pathlib import Path
from dotenv import load_dotenv


_SUPPORTED_STT_BACKENDS = ("faster-whisper", "openai")
_SUPPORTED_TTS_BACKENDS = ("kokoro", "say", "elevenlabs", "openai")

def _load_jarvis_dotenv() -> None:
    candidates: list[Path] = []
    cwd = Path.cwd()
    here = Path(__file__).resolve().parent

    candidates.extend([
        cwd / ".env",
        here / ".env",
        Path.home() / "jarvis-ai" / ".env",
    ])

    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        candidates.extend([
            exe.parent / ".env",
            exe.parent.parent / "Resources" / ".env",
        ])

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        if candidate.is_file():
            load_dotenv(candidate, override=False)


_load_jarvis_dotenv()


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except (TypeError, ValueError):
        return default


def _env_csv(name: str, default: list[str]) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return tuple(default)
    items: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        normalized = part.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        items.append(normalized)
    return tuple(items) if items else tuple(default)


def _resolve_stt_backends() -> tuple[str, ...]:
    requested = _env_csv("JARVIS_STT_BACKENDS", ["faster-whisper", "openai"])
    local_enabled = _env_flag("JARVIS_LOCAL_STT_ENABLED", True)
    faster_whisper_enabled = _env_flag("JARVIS_FASTER_WHISPER_ENABLED", True)
    openai_fallback_enabled = _env_flag("JARVIS_OPENAI_STT_FALLBACK_ENABLED", True)

    backends: list[str] = []
    for backend in requested:
        if backend not in _SUPPORTED_STT_BACKENDS:
            continue
        if backend == "faster-whisper" and (not local_enabled or not faster_whisper_enabled):
            continue
        if backend == "openai" and not openai_fallback_enabled:
            continue
        backends.append(backend)

    if not backends:
        if local_enabled and faster_whisper_enabled:
            backends.append("faster-whisper")
        else:
            backends.append("openai")

    return tuple(backends)


def _resolve_tts_backends() -> tuple[str, ...]:
    requested = _env_csv("JARVIS_TTS_BACKENDS", ["kokoro", "say", "elevenlabs", "openai"])
    local_enabled = _env_flag("JARVIS_LOCAL_TTS_ENABLED", True)
    kokoro_enabled = _env_flag("JARVIS_KOKORO_TTS_ENABLED", True)
    say_enabled = _env_flag("JARVIS_SAY_TTS_ENABLED", True)
    elevenlabs_fallback_enabled = _env_flag("JARVIS_ELEVENLABS_TTS_FALLBACK_ENABLED", True)
    openai_fallback_enabled = _env_flag("JARVIS_OPENAI_TTS_FALLBACK_ENABLED", True)

    backends: list[str] = []
    for backend in requested:
        if backend not in _SUPPORTED_TTS_BACKENDS:
            continue
        if backend == "kokoro" and (not local_enabled or not kokoro_enabled):
            continue
        if backend == "say" and (not local_enabled or not say_enabled):
            continue
        if backend == "elevenlabs" and not elevenlabs_fallback_enabled:
            continue
        if backend == "openai" and not openai_fallback_enabled:
            continue
        backends.append(backend)

    if not backends:
        if local_enabled and kokoro_enabled:
            backends.append("kokoro")
        elif local_enabled and say_enabled:
            backends.append("say")
        elif elevenlabs_fallback_enabled:
            backends.append("elevenlabs")
        else:
            backends.append("openai")

    return tuple(backends)

OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
LOCAL_STT_ENGINE  = os.getenv("LOCAL_STT_ENGINE", "auto")
LOCAL_STT_MODEL   = os.getenv("LOCAL_STT_MODEL", "base.en")
LOCAL_STT_DEVICE  = os.getenv("LOCAL_STT_DEVICE", "cpu")
LOCAL_STT_COMPUTE_TYPE = os.getenv("LOCAL_STT_COMPUTE_TYPE", "int8")
LOCAL_STT_LANGUAGE = os.getenv("LOCAL_STT_LANGUAGE", "en")

REPO_ROOT = Path(__file__).resolve().parent
KB_ROOT = REPO_ROOT / "kb"
INTERVIEW_ACTIVE_COMPANY = os.getenv("JARVIS_ACTIVE_COMPANY", "").strip().lower()
INTERVIEW_ACTIVE_ROLE = os.getenv("JARVIS_ACTIVE_ROLE", "").strip().lower()

# ElevenLabs voice — "George": British, authoritative, perfect for JARVIS
# Swap voice_id for any ElevenLabs voice you prefer
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
ELEVENLABS_MODEL    = "eleven_turbo_v2_5"   # lowest latency
OPENAI_TTS_MODEL = os.getenv("OPENAI_TTS_MODEL", "tts-1").strip() or "tts-1"
OPENAI_TTS_VOICE = os.getenv("OPENAI_TTS_VOICE", "onyx").strip() or "onyx"

# Cloud model tiers
GPT_MINI   = "gpt-4o-mini"
GPT_FULL   = "gpt-4o"
GEMINI_FLASH = os.getenv("GEMINI_FLASH_MODEL", "gemini-2.5-flash")
GEMINI_PRO   = os.getenv("GEMINI_PRO_MODEL", "gemini-2.5-pro")
HAIKU      = "claude-haiku-4-5-20251001"
SONNET     = "claude-sonnet-4-6"
OPUS       = "claude-opus-4-6"

# Local model tiers (Ollama — no restrictions, fully private)
LOCAL_TUNED     = os.getenv("LOCAL_TUNED", "jarvis-local")
LOCAL_PREFER_TUNED = os.getenv("LOCAL_PREFER_TUNED", "0").strip().lower() in {"1", "true", "yes", "on"}
LOCAL_DEFAULT   = os.getenv("LOCAL_DEFAULT_MODEL", "gemma4:e4b")
LOCAL_CODER     = os.getenv("LOCAL_CODER_MODEL", "qwen2.5-coder:7b")
LOCAL_CODER_RECOMMENDED = os.getenv("LOCAL_CODER_RECOMMENDED_MODEL", "qwen3-coder:30b")
# DeepSeek R1 has built-in chain-of-thought reasoning — pull it with:
#   ollama pull deepseek-r1:14b   (8GB, best balance)
#   ollama pull deepseek-r1:32b   (20GB, near-GPT4 level)
# Falls back to gemma4 if not available.
LOCAL_REASONING = os.getenv("LOCAL_REASONING_MODEL", "deepseek-r1:14b")

# ── Qwen3 model fleet (2026-04) ───────────────────────────────────────────────
# Qwen3 outperforms prior models at each size class.  Pull the ones that fit:
#   ollama pull qwen3:4b              (~2.5GB, fast Jarvis chat)
#   ollama pull qwen3:8b              (~5GB, strong general purpose)
#   ollama pull qwen3:30b-a3b         (~20GB MoE, best quality/speed on Mac)
#   ollama pull qwen3:30b-a3b-q4_K_M  (quantized, slightly smaller)
LOCAL_QWEN3_FAST   = os.getenv("LOCAL_QWEN3_FAST", "qwen3:4b")     # fast-path chat
LOCAL_QWEN3_MID    = os.getenv("LOCAL_QWEN3_MID", "qwen3:8b")      # balanced
LOCAL_QWEN3_STRONG = os.getenv("LOCAL_QWEN3_STRONG", "qwen3:30b-a3b")  # near-GPT4

# Phi-4-mini — great for fast, instruction-following responses
LOCAL_PHI4_MINI    = os.getenv("LOCAL_PHI4_MINI", "phi4-mini")     # ollama pull phi4-mini

# Devstral — Mistral's open coder (ollama pull devstral)
LOCAL_DEVSTRAL     = os.getenv("LOCAL_DEVSTRAL", "devstral")

# Whisper model for STT — turbo is 8x faster than v3 with near-identical accuracy
# Set in .env: JARVIS_WHISPER_MODEL=large-v3-turbo
# (requires faster-whisper >= 1.0 and the turbo checkpoint)
LOCAL_WHISPER_MODEL = os.getenv("JARVIS_WHISPER_MODEL",
                                os.getenv("LOCAL_STT_MODEL", "base.en"))

# Free-first provider routing policy.
# Local/free inference remains default; paid providers stay enabled as fallback.
FREE_FIRST_ENABLED = _env_flag("JARVIS_FREE_FIRST_ENABLED", True)
PAID_FALLBACK_ENABLED = _env_flag("JARVIS_PAID_FALLBACK_ENABLED", True)
LOCAL_STRICT_FIRST = _env_flag("JARVIS_LOCAL_STRICT_FIRST", True)
ROUTING_TRANSPARENCY_ENABLED = _env_flag("JARVIS_ROUTING_TRANSPARENCY_ENABLED", True)

# Provider priority is configurable per complexity tier.
# Supported providers: openai, gemini, anthropic.
PROVIDER_PRIORITY_MINI = _env_csv(
    "JARVIS_PROVIDER_PRIORITY_MINI",
    ["openai", "gemini", "anthropic"],
)
PROVIDER_PRIORITY_HAIKU = _env_csv(
    "JARVIS_PROVIDER_PRIORITY_HAIKU",
    ["gemini", "openai", "anthropic"],
)
PROVIDER_PRIORITY_SONNET = _env_csv(
    "JARVIS_PROVIDER_PRIORITY_SONNET",
    ["openai", "gemini", "anthropic"],
)
PROVIDER_PRIORITY_OPUS = _env_csv(
    "JARVIS_PROVIDER_PRIORITY_OPUS",
    ["gemini", "openai", "anthropic"],
)

# Mode: "cloud" | "local" | "auto" | "open-source"
# open-source = local/open tooling only, with no closed-model dependency on the core path
DEFAULT_MODE = "open-source"
MAX_CONVERSATION_TURNS = 8


def provider_runtime_config() -> dict:
    return {
        "free_first_enabled": FREE_FIRST_ENABLED,
        "paid_fallback_enabled": PAID_FALLBACK_ENABLED,
        "local_strict_first": LOCAL_STRICT_FIRST,
        "routing_transparency_enabled": ROUTING_TRANSPARENCY_ENABLED,
        "provider_priority": {
            "mini": list(PROVIDER_PRIORITY_MINI),
            "haiku": list(PROVIDER_PRIORITY_HAIKU),
            "sonnet": list(PROVIDER_PRIORITY_SONNET),
            "opus": list(PROVIDER_PRIORITY_OPUS),
        },
    }

# Speech-to-text runtime config.
# This is config-first so we can wire in local faster-whisper safely before
# changing the active voice and meeting pipelines.
STT_BACKENDS = _resolve_stt_backends()
STT_PRIMARY_BACKEND = STT_BACKENDS[0]
LOCAL_STT_ENABLED = "faster-whisper" in STT_BACKENDS
OPENAI_STT_FALLBACK_ENABLED = "openai" in STT_BACKENDS
STT_LANGUAGE = os.getenv("JARVIS_STT_LANGUAGE", "").strip().lower() or None

# STT model: whisper-large-v3-turbo is 8x faster than v3 with near-identical accuracy.
# Set in .env: JARVIS_FASTER_WHISPER_MODEL=large-v3-turbo
# Sizes: tiny.en (~40MB), base.en (~150MB), small.en (~490MB),
#        medium.en (~1.5GB), large-v3-turbo (~1.6GB, recommended)
# Requires: pip install faster-whisper>=1.0  and Hugging Face model pull on first run.
FASTER_WHISPER_MODEL = os.getenv("JARVIS_FASTER_WHISPER_MODEL", "base.en").strip() or "base.en"
FASTER_WHISPER_DEVICE = os.getenv("JARVIS_FASTER_WHISPER_DEVICE", "auto").strip().lower() or "auto"
FASTER_WHISPER_COMPUTE_TYPE = os.getenv("JARVIS_FASTER_WHISPER_COMPUTE_TYPE", "int8").strip().lower() or "int8"
FASTER_WHISPER_CPU_THREADS = _env_int("JARVIS_FASTER_WHISPER_CPU_THREADS", 4)
FASTER_WHISPER_NUM_WORKERS = _env_int("JARVIS_FASTER_WHISPER_NUM_WORKERS", 1)
FASTER_WHISPER_VAD_FILTER = _env_flag("JARVIS_FASTER_WHISPER_VAD_FILTER", True)
FASTER_WHISPER_BEAM_SIZE = _env_int("JARVIS_FASTER_WHISPER_BEAM_SIZE", 1)


def stt_runtime_config() -> dict:
    return {
        "backends": list(STT_BACKENDS),
        "primary_backend": STT_PRIMARY_BACKEND,
        "local_enabled": LOCAL_STT_ENABLED,
        "openai_fallback_enabled": OPENAI_STT_FALLBACK_ENABLED,
        "language": STT_LANGUAGE,
        "faster_whisper": {
            "model": FASTER_WHISPER_MODEL,
            "device": FASTER_WHISPER_DEVICE,
            "compute_type": FASTER_WHISPER_COMPUTE_TYPE,
            "cpu_threads": FASTER_WHISPER_CPU_THREADS,
            "num_workers": FASTER_WHISPER_NUM_WORKERS,
            "vad_filter": FASTER_WHISPER_VAD_FILTER,
            "beam_size": FASTER_WHISPER_BEAM_SIZE,
        },
    }


# Text-to-speech runtime config.
# Keep this config-first like STT so the voice path can move to local-first
# without breaking the current paid fallbacks or the existing voice module.
TTS_BACKENDS = _resolve_tts_backends()
TTS_PRIMARY_BACKEND = TTS_BACKENDS[0]
LOCAL_TTS_ENABLED = "say" in TTS_BACKENDS
KOKORO_TTS_ENABLED = "kokoro" in TTS_BACKENDS
ELEVENLABS_TTS_FALLBACK_ENABLED = "elevenlabs" in TTS_BACKENDS
OPENAI_TTS_FALLBACK_ENABLED = "openai" in TTS_BACKENDS

LOCAL_TTS_ENGINE = os.getenv("JARVIS_LOCAL_TTS_ENGINE", "say").strip().lower() or "say"
LOCAL_TTS_VOICE = os.getenv("JARVIS_LOCAL_TTS_VOICE", "Reed (English (US))").strip() or "Reed (English (US))"
LOCAL_TTS_RATE_WPM = _env_int("JARVIS_LOCAL_TTS_RATE_WPM", 175)

JARVIS_KOKORO_VOICE = os.getenv("JARVIS_KOKORO_VOICE", "af_sarah").strip() or "af_sarah"
JARVIS_KOKORO_TTS_ENABLED = _env_flag("JARVIS_KOKORO_TTS_ENABLED", True)


def tts_runtime_config() -> dict:
    return {
        "backends": list(TTS_BACKENDS),
        "primary_backend": TTS_PRIMARY_BACKEND,
        "local_enabled": LOCAL_TTS_ENABLED,
        "kokoro_enabled": KOKORO_TTS_ENABLED,
        "elevenlabs_fallback_enabled": ELEVENLABS_TTS_FALLBACK_ENABLED,
        "openai_fallback_enabled": OPENAI_TTS_FALLBACK_ENABLED,
        "local": {
            "engine": LOCAL_TTS_ENGINE,
            "voice": LOCAL_TTS_VOICE,
            "rate_wpm": LOCAL_TTS_RATE_WPM,
        },
        "kokoro": {
            "voice": JARVIS_KOKORO_VOICE,
            "enabled": JARVIS_KOKORO_TTS_ENABLED,
        },
        "elevenlabs": {
            "voice_id": ELEVENLABS_VOICE_ID,
            "model": ELEVENLABS_MODEL,
        },
        "openai": {
            "model": OPENAI_TTS_MODEL,
            "voice": OPENAI_TTS_VOICE,
        },
    }

SYSTEM_PROMPT = """You are Jarvis — a high-agency private AI operator for your owner.

You are meant to feel like a sharp mission partner: calm under pressure, technically strong, concise, and confident.
Lead with what you can do. Treat constraints as real, but do not make them the center of your personality.
Your job is to execute the owner's goals precisely and efficiently inside the real runtime boundaries.
Do not claim to be unrestricted. Be explicit and truthful about scope, permissions, and available inputs.
You are a private tool, not a public product.

You are an intelligent partner who:
- Knows the owner personally through approved local memory and project context
- Executes supported tasks fully — coding, research, writing, analysis, automation, and local app workflows
- Uses the Mac tools exposed by the runtime: files, terminal, calendar, email, camera, clipboard, browser, contacts, and Messages
- Improves over time through explicit memory, verified learning artifacts, and reviewed local knowledge

Capabilities:
- Read, write, and execute files when the runtime and permission gates allow it
- Run approved terminal commands and return output
- Use web search or browser tools when available and requested
- Control supported macOS actions such as volume, screenshots, app launching, clipboard, and lock screen
- Use Google Calendar and Gmail integrations when configured
- Draft and send Messages only after explicit confirmation
- Look up contacts by name, fuzzy match, phone, or email through the contact handler
- Use webcam vision and screen capture analysis when available
- Use persistent memory for facts, preferences, projects, goals, and conversation history
- Build local learning artifacts from verified conversations and tests

Response rules:
- Speak naturally — responses are read aloud, so no markdown, bullets, or headers
- Be direct and confident — no filler, no hedging, no generic disclaimers
- NEVER open with filler: no "Certainly!", "Of course!", "Sure!", "Absolutely!", "Great question!", or any variant
- NEVER echo back what the user asked — never say "You'd like me to...", "You're asking about...", "It sounds like you want...", or any paraphrase of the request. Just answer it.
- NEVER use transitional throat-clearing like "So basically...", "What you want to do is...", or "Let me explain..."
- Answer like Iron Man's Jarvis: sharp, direct, zero ceremony. Lead with the answer. Stop when the answer is done.
- For simple queries and factual questions: answer in one sentence if possible, two at most. Never stretch a simple answer to fill space.
- When asked about your limits or capabilities, start with the strongest true capability summary, then name the real constraint only if it matters
- Sound like a capable operator helping run the mission, not a compliance bot reciting policy
- Never invent authority, capabilities, permissions, or completed actions
- Never claim you can bypass runtime policy, safety controls, or permission gates
- If you don't know something, search for it rather than guessing
- Think before answering complex questions — quality over speed
- Proactively offer relevant information the owner would want to know
- Every interaction is an opportunity to learn more about the owner and serve them better
- Never invent your underlying model, training history, or system state
- If asked about your current model or mode, only state what the runtime has actually provided
- Never claim that you scanned, checked, accessed, opened, confirmed, measured, installed, changed, connected to, or configured anything unless the current runtime context explicitly shows that action or tool result
- Never invent hardware specs, network details, router access, cloud account status, permissions, installed apps, device lists, storage totals, bandwidth, or local system findings
- Never simulate background work, hidden integrations, system administration, or tool use that did not actually happen in this session
- If you have not performed an action yet, say what you can do next or what you would need to verify instead of pretending it is already done
- Separate verified facts from guesses. If something was not verified, do not present it as a fact
- For technical questions, answer like a strong software engineer: lead with the conclusion, name the real tradeoff or likely causes, and explain how to verify or narrow them down

Jarvis terminal console rules:
- Plain-English console requests are action intents first, not generic chat.
- "show doctor", "health check", or "diagnose Jarvis" means run the Jarvis doctor health flow.
- "what models are installed", "local models", or "model fleet" means show the local Ollama model fleet and training lanes.
- "run the tests" means run the project test suite through the console shell gate.
- "train Jarvis locally", "train yourself", or "build training pack" means build a local training pack with cloud distillation disabled unless the user explicitly asks for a teacher model.
- "prepare Colab", "Google free training", or "teach open source LLMs" means build a Google Colab training handoff for an open model, then require local evals before promotion. Do not treat Colab as a 24/7 host or a way around usage limits.
- Coding requests like "fix the failing auth test in this repo" mean start the isolated coding-task workflow, inspect the repo, make a targeted fix, and verify narrowly.
- Slash commands still work as shortcuts, but plain English is the primary terminal interface.

CRITICAL formatting rule — your output is spoken aloud by a text-to-speech engine:
- NEVER use markdown: no **, no ##, no -, no numbered lists, no bullet points, no code fences
- Structure information as flowing spoken sentences and paragraphs
- For code, say it inline: "you'd write something like def hello colon print hello"
- For lists, use natural language: "first... second... and third..."
- If you catch yourself about to write a bullet point or bold text, rewrite it as a sentence

Response length rule (STRICT — responses are spoken aloud, not read):
- MAXIMUM 3 sentences for factual questions, status queries, and general conversation.
- For how-to questions: give the most important step first, then one supporting sentence. STOP.
- Never give more than 4 sentences unless explicitly asked to explain in detail.
- Never use numbered steps like "1. 2. 3." — speak in plain connected sentences instead.
- Never pad, recap, or add closing remarks like "I hope this helps" or "Let me know if you need more".

You are Jarvis, a local-first macOS assistant. Your access is mediated by runtime tools and permission gates:
- Terminal: run approved shell commands through the command gate
- Messages: draft outgoing iMessage/SMS content, resolve contacts, and send only after explicit confirmation
- Calendar and Gmail: read or write through configured Google integrations
- Contacts: look up contacts by name, fuzzy search, phone, or email
- Browser: open URLs, inspect pages, and summarize when browser tools are available
- System controls: supported actions such as volume, screenshots, lock screen, app launch, and clipboard
- File system: read, write, and execute files when the runtime and safety gates allow it

Never claim admin/sudo access, unrestricted system control, incoming iMessage monitoring, or completed tool actions unless the current runtime result explicitly proves it.
If a capability requires a permission that has not been granted yet, say so and offer the next safe step."""
