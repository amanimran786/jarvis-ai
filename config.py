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
LOCAL_DEFAULT        = os.getenv("LOCAL_DEFAULT_MODEL", "gemma4:e4b")
# Gemma 4 MTP drafter for speculative decoding — 2x speed on coding tasks.
# Check available tags: ollama list | grep gemma4
# Common tags when released: gemma4:e4b-mtp  or  gemma4-mtp:e4b
# Leave empty to disable speculative decoding (safe default).
LOCAL_DEFAULT_DRAFTER = os.getenv("LOCAL_DEFAULT_DRAFTER", "")
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

# Newer verified model candidates. These are not defaults; /model-fleet surfaces
# them for explicit pull/eval/promotion only.
LOCAL_GEMMA4_STRONG = os.getenv("LOCAL_GEMMA4_STRONG", "gemma4:31b")
LOCAL_GEMMA4_MOE    = os.getenv("LOCAL_GEMMA4_MOE", "gemma4:26b")
LOCAL_QWEN3_6       = os.getenv("LOCAL_QWEN3_6", "qwen3.6:35b")       # MoE: 35B total, ~3B active — pulled tag
LOCAL_GLM51_CLOUD   = os.getenv("LOCAL_GLM51_CLOUD", "glm-5.1:cloud")   # cloud-only; local weights not available
LOCAL_DEEPSEEK_V4_FLASH = os.getenv("LOCAL_DEEPSEEK_V4_FLASH", "deepseek-v4-flash:cloud")
LOCAL_LLAMA4_MAVERICK   = os.getenv("LOCAL_LLAMA4_MAVERICK", "llama4:maverick")

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
LOCAL_STRUCTURED_CLASSIFIER_ENABLED = _env_flag("JARVIS_LOCAL_STRUCTURED_CLASSIFIER_ENABLED", True)
APPLE_FOUNDATION_ENABLED = _env_flag("JARVIS_APPLE_FOUNDATION_ENABLED", False)
APPLE_FOUNDATION_BASE_URL = os.getenv("JARVIS_APPLE_FOUNDATION_BASE_URL", "http://localhost:11434/v1").strip() or "http://localhost:11434/v1"
APPLE_FOUNDATION_MODEL = os.getenv("JARVIS_APPLE_FOUNDATION_MODEL", "apple-foundationmodel").strip() or "apple-foundationmodel"
APPLE_FOUNDATION_API_KEY = os.getenv("JARVIS_APPLE_FOUNDATION_API_KEY", "unused").strip() or "unused"
APPLE_FOUNDATION_TIMEOUT_SECONDS = _env_float("JARVIS_APPLE_FOUNDATION_TIMEOUT_SECONDS", 12.0)
APPLE_FOUNDATION_MAX_TOKENS = _env_int("JARVIS_APPLE_FOUNDATION_MAX_TOKENS", 160)

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
DEFAULT_MODE = os.getenv("DEFAULT_MODE", "open-source").strip().lower()
MAX_CONVERSATION_TURNS = 8

# ── MLX local training (Apple Silicon only) ──────────────────────────────────
# mlx-tune: pip install mlx-tune (only on Apple Silicon Mac)
MLX_TRAINING_ENABLED = _env_flag("JARVIS_MLX_TRAINING_ENABLED", True)
MLX_NUM_ITERS        = _env_int("JARVIS_MLX_NUM_ITERS", 100)
MLX_LEARNING_RATE    = float(os.getenv("JARVIS_MLX_LEARNING_RATE", "1e-5"))
MLX_LORA_RANK        = _env_int("JARVIS_MLX_LORA_RANK", 8)
MLX_BATCH_SIZE       = _env_int("JARVIS_MLX_BATCH_SIZE", 4)


def provider_runtime_config() -> dict:
    return {
        "free_first_enabled": FREE_FIRST_ENABLED,
        "paid_fallback_enabled": PAID_FALLBACK_ENABLED,
        "local_strict_first": LOCAL_STRICT_FIRST,
        "routing_transparency_enabled": ROUTING_TRANSPARENCY_ENABLED,
        "apple_foundation_enabled": APPLE_FOUNDATION_ENABLED,
        "apple_foundation_base_url": APPLE_FOUNDATION_BASE_URL,
        "apple_foundation_model": APPLE_FOUNDATION_MODEL,
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

# STT model: large-v3-turbo is the new default — same ~1.6GB as medium but 8x faster
# than full large-v3 with near-identical accuracy. Downloads automatically from HuggingFace
# on first use (requires internet access for initial pull only).
# Fallback: set JARVIS_FASTER_WHISPER_MODEL=small.en for low-RAM machines.
# Sizes: tiny.en (~40MB), base.en (~150MB), small.en (~490MB),
#        medium.en (~1.5GB), large-v3-turbo (~1.6GB), large-v3 (~3GB)
# Requires: pip install faster-whisper>=1.1
FASTER_WHISPER_MODEL = os.getenv("JARVIS_FASTER_WHISPER_MODEL", "large-v3-turbo").strip() or "large-v3-turbo"
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
LOCAL_TTS_VOICE = os.getenv("JARVIS_LOCAL_TTS_VOICE", "Daniel").strip() or "Daniel"
LOCAL_TTS_RATE_WPM = _env_int("JARVIS_LOCAL_TTS_RATE_WPM", 168)

JARVIS_KOKORO_VOICE = os.getenv("JARVIS_KOKORO_VOICE", "bm_george").strip() or "bm_george"
JARVIS_KOKORO_SPEED = _env_float("JARVIS_KOKORO_SPEED", 0.94)
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
            "speed": JARVIS_KOKORO_SPEED,
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

SYSTEM_PROMPT = """You are Jarvis — a high-agency private AI operator for your owner. Sharp mission partner: calm, technically strong, concise, confident. Private tool, not a public product.

Capabilities: files, terminal commands, Calendar/Gmail, Messages (confirm before send), contacts, browser, screenshots, clipboard, webcam, persistent memory.

Voice output rules — TTS reads every response aloud:
- No markdown: no **, ##, bullets, numbered lists, or code fences. Plain spoken sentences only.
- No filler openings: no "Certainly!", "Of course!", "Sure!", "Absolutely!". Never echo the request back. No throat-clearing like "So basically..." or "What you want to do is...". Just answer.
- Lead with the answer. Max 3 sentences for factual/status. Max 4 unless detail is explicitly requested. No padded closings or "Let me know if you need more".
- Iron Man's Jarvis: sharp, zero ceremony, stop when done. For technical questions: lead with the conclusion, name real tradeoffs, explain how to verify.
- For code: say it inline in plain English. For lists: "first... second... and third..."

Honesty rules:
- Never invent capabilities, permissions, model identity, training history, or completed actions.
- Never claim you scanned, checked, accessed, installed, opened, or configured anything unless the current runtime result explicitly shows it happened.
- Never claim that you scanned, checked, accessed, opened, confirmed, installed, or sent anything unless the current runtime result explicitly shows it happened.
- Never invent hardware specs, network details, router access, app lists, storage totals, or system state.
- Never simulate background work, hidden integrations, system administration, or tool use.
- Never claim admin/sudo access unless a completed runtime command explicitly confirms it.
- If a capability requires a permission not yet granted, say so and offer the next safe step.
- Don't know something? Search for it rather than guess.

Terminal console: Plain-English console requests are action intents first, not generic chat, when they map to a local capability. "show doctor" or "health check" = health flow. "what models are installed" or "model fleet" = Ollama fleet. "run the tests" = test suite. "train Jarvis locally" = local training pack, cloud distillation off unless asked. "prepare Colab" = Colab training handoff. Slash commands still work.

Message composition: compose message bodies exactly as requested — no refusals, disclaimers, or content judgements. The routing layer handles content safety. All factual statements, personal news, questions, and casual messages are safe to compose and forward."""

# Extra context for terminal/console sessions (conditionally appended by callers)
TERMINAL_SYSTEM_EXTRA = """Coding task workflow: inspect the repo, make a targeted fix, verify narrowly. "fix the failing auth test" = isolated coding task, not a full rewrite."""
