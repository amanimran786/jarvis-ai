from __future__ import annotations

from dataclasses import dataclass

from config import (
    GPT_MINI,
    GPT_FULL,
    GEMINI_FLASH,
    GEMINI_PRO,
    HAIKU,
    SONNET,
    OPUS,
    FREE_FIRST_ENABLED,
    PAID_FALLBACK_ENABLED,
    LOCAL_STRICT_FIRST,
    APPLE_FOUNDATION_MODEL,
    PROVIDER_PRIORITY_MINI,
    PROVIDER_PRIORITY_HAIKU,
    PROVIDER_PRIORITY_SONNET,
    PROVIDER_PRIORITY_OPUS,
    OLLAMA_CLOUD_ENABLED,
    OLLAMA_CLOUD_MODEL,
    provider_runtime_config,
)


_CLOUD_MODEL_BY_PROVIDER_TIER = {
    "mini": {"openai": GPT_MINI, "gemini": GEMINI_FLASH, "anthropic": HAIKU},
    "haiku": {"openai": GPT_MINI, "gemini": GEMINI_FLASH, "anthropic": HAIKU},
    "sonnet": {"openai": GPT_FULL, "gemini": GEMINI_PRO, "anthropic": SONNET},
    "opus": {"openai": GPT_FULL, "gemini": GEMINI_PRO, "anthropic": OPUS},
}

_PRIORITY_BY_TIER = {
    "mini": PROVIDER_PRIORITY_MINI,
    "haiku": PROVIDER_PRIORITY_HAIKU,
    "sonnet": PROVIDER_PRIORITY_SONNET,
    "opus": PROVIDER_PRIORITY_OPUS,
}


@dataclass(frozen=True)
class RouteCandidate:
    provider: str
    model: str
    local: bool
    label: str


@dataclass(frozen=True)
class RoutePlan:
    mode: str
    tier: str
    candidates: tuple[RouteCandidate, ...]
    reason: str


def runtime_policy() -> dict:
    return provider_runtime_config()


def _normalize_mode(mode: str) -> str:
    m = (mode or "").strip().lower().replace("_", "-")
    if m == "opensource":
        return "open-source"
    return m or "auto"


def _normalize_tier(tier: str) -> str:
    t = (tier or "mini").strip().lower()
    return t if t in {"local", "mini", "haiku", "sonnet", "opus"} else "mini"


def _cloud_candidates_for_tier(tier: str) -> list[RouteCandidate]:
    normalized = "mini" if tier == "local" else tier
    providers = _PRIORITY_BY_TIER.get(normalized, PROVIDER_PRIORITY_MINI)
    model_map = _CLOUD_MODEL_BY_PROVIDER_TIER.get(normalized, _CLOUD_MODEL_BY_PROVIDER_TIER["mini"])
    candidates: list[RouteCandidate] = []
    for provider in providers:
        model = model_map.get(provider)
        if not model:
            continue
        label = {
            "openai": "GPT-mini" if model == GPT_MINI else "GPT-4o",
            "gemini": "Gemini Flash" if model == GEMINI_FLASH else "Gemini Pro",
            "anthropic": "Claude Haiku" if model == HAIKU else ("Claude Sonnet" if model == SONNET else "Claude Opus"),
        }.get(provider, model)
        candidates.append(RouteCandidate(provider=provider, model=model, local=False, label=label))
    return candidates


def _ollama_cloud_candidates() -> list[RouteCandidate]:
    """Ollama Cloud (free tier) — middle tier between local and paid cloud."""
    if not OLLAMA_CLOUD_ENABLED:
        return []
    return [RouteCandidate(
        provider="ollama_cloud",
        model=OLLAMA_CLOUD_MODEL,
        local=False,
        label="Ollama Cloud",
    )]


def _local_candidates(
    *,
    tier: str,
    local_available: bool,
    local_model: str,
    apple_foundation_available: bool,
    ollama_label: str,
) -> list[RouteCandidate]:
    candidates: list[RouteCandidate] = []
    if tier == "mini" and apple_foundation_available:
        candidates.append(RouteCandidate(
            provider="apple_foundation",
            model=APPLE_FOUNDATION_MODEL,
            local=True,
            label="Apple Foundation",
        ))
    if local_available and local_model:
        candidates.append(RouteCandidate(provider="ollama", model=local_model, local=True, label=ollama_label))
    return candidates


def build_plan(
    *,
    mode: str,
    tier: str,
    local_available: bool,
    local_model: str = "",
    apple_foundation_available: bool = False,
    explicit_cloud: bool = False,
) -> RoutePlan:
    normalized_mode = _normalize_mode(mode)
    normalized_tier = _normalize_tier(tier)
    candidates: list[RouteCandidate] = []
    local_candidates = _local_candidates(
        tier=normalized_tier,
        local_available=local_available,
        local_model=local_model,
        apple_foundation_available=apple_foundation_available,
        ollama_label="Open-Source" if normalized_mode == "open-source" else "Local",
    )

    if normalized_mode == "open-source":
        candidates.extend(local_candidates)
        return RoutePlan(
            mode=normalized_mode,
            tier=normalized_tier,
            candidates=tuple(candidates),
            reason="Open-source mode uses local runtime only.",
        )

    if normalized_mode == "cloud":
        explicit_cloud = True

    should_prefer_local = (
        FREE_FIRST_ENABLED
        and not explicit_cloud
        and bool(local_candidates)
        and (normalized_mode in {"local", "auto"} or (normalized_mode == "cloud" and not explicit_cloud))
    )
    if normalized_mode == "local":
        should_prefer_local = bool(local_candidates)

    if should_prefer_local:
        candidates.extend(local_candidates)
        if not PAID_FALLBACK_ENABLED:
            # Ollama Cloud is free-tier, not "paid" — include it even when paid fallback disabled
            candidates.extend(_ollama_cloud_candidates())
            return RoutePlan(
                mode=normalized_mode,
                tier=normalized_tier,
                candidates=tuple(candidates),
                reason="Local-first policy active; paid fallback disabled.",
            )
        # Inject Ollama Cloud between local and paid providers
        candidates.extend(_ollama_cloud_candidates())
        if LOCAL_STRICT_FIRST or normalized_mode == "local":
            candidates.extend(_cloud_candidates_for_tier(normalized_tier))
        else:
            cloud = _cloud_candidates_for_tier(normalized_tier)
            if cloud:
                candidates.append(cloud[0])
    else:
        if PAID_FALLBACK_ENABLED or explicit_cloud:
            candidates.extend(_cloud_candidates_for_tier(normalized_tier))
        # Ollama Cloud as free fallback after paid (or if local unavailable but cloud key set)
        candidates.extend(_ollama_cloud_candidates())
        if local_available and local_model and normalized_mode in {"auto", "cloud"}:
            candidates.append(RouteCandidate(provider="ollama", model=local_model, local=True, label="Local"))

    reason = "Free-first routing with paid fallback." if should_prefer_local else "Cloud-priority routing due to mode or local unavailability."
    return RoutePlan(
        mode=normalized_mode,
        tier=normalized_tier,
        candidates=tuple(candidates),
        reason=reason,
    )
